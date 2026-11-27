#!/usr/bin/env python3
"""전주시 한파특보를 조회하여 저장소 루트의 weather.json을 갱신합니다.

GitHub 경로: scripts/weather.py / 인증키: Repository secret KMA_API_KEY
Python 표준 라이브러리만 사용합니다. 기존 weather.yml과 함께 쓸 수 있습니다.

공식 명세 확인: 2026-11-28
https://apihub.kma.go.kr/apiList.do?seqApi=10&seqApiSub=288
https://apihub.kma.go.kr/static/html/attach/wrn_table.html

조회 실패 시 unknown을 저장하고 정상 종료하여 기존 Publish status 단계가
실행되도록 합니다. 조회 성공 여부는 KMA RESULT 로그 및 작업 요약에 표시합니다.
인증키, 요청 URL, 원문 응답은 로그나 weather.json에 기록하지 않습니다.
"""

import http.client
import json
import os
import re
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path


ENDPOINT = "https://apihub.kma.go.kr/api/typ01/url/wrn_now_data.php"
REGION_ID = "L1061300"  # 기상청 공식 특보구역 코드: 전주
REGION_NAMES = {"전주", "전주시"}
KST = timezone(timedelta(hours=9))
TIMEOUT_SECONDS = 55
MAX_ATTEMPTS = 3
MAX_RESPONSE_BYTES = 2 * 1024 * 1024
REQUIRED = {"REG_ID", "REG_KO", "TM_FC", "TM_EF", "WRN", "LVL", "CMD"}
WRAPPERS = {"DATA", "ITEMS", "ITEM", "ROWS", "RECORDS", "RESPONSE", "BODY", "RESULT", "RESULTS"}
WARNING_TYPES = {
    "강풍": "W", "호우": "R", "한파": "C", "건조": "D", "폭풍해일": "O",
    "해일": "O", "지진해일": "N", "풍랑": "V", "태풍": "T", "대설": "S",
    "황사": "Y", "폭염": "H", "안개": "F", "열대야": "K",
}
LEVELS = {"예비특보": "1", "주의보": "2", "경보": "3"}
COMMANDS = {"발표": "1", "대치": "2", "해제": "3", "대치해제": "4", "연장": "5", "변경": "6", "변경해제": "7"}
ACTIVE_COMMANDS = {"1", "2", "5", "6"}
CANCEL_COMMANDS = {"3", "4", "7"}
LABELS = {"none": "한파특보 미발효", "advisory": "한파주의보", "warning": "한파경보", "unknown": "조회 불가 · 담당자 확인 필요"}


class LookupFailure(Exception):
    """인증키를 포함하지 않는 고정 코드와 안내문만 전달합니다."""

    def __init__(self, code, message, retryable=False):
        super().__init__(code)
        self.code, self.message, self.retryable = code, message, retryable


def utc_now():
    return datetime.now(timezone.utc)


def fail(code, message):
    raise LookupFailure(code, message)


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # 인증키가 들어간 요청을 다른 주소로 넘기지 않습니다.
        return None


def fetch_document(key, opener=None, sleeper=time.sleep):
    params = {"fe": "e", "disp": "1", "authKey": key}
    request = urllib.request.Request(
        ENDPOINT + "?" + urllib.parse.urlencode(params),
        headers={"User-Agent": "Jeonju-Osung-Weather/2.0", "Accept": "application/json", "Accept-Encoding": "identity"},
    )
    opener = opener or urllib.request.build_opener(NoRedirect())
    for attempt in range(1, MAX_ATTEMPTS + 1):
        print(f"KMA request {attempt}/{MAX_ATTEMPTS} (timeout {TIMEOUT_SECONDS}s)", flush=True)
        try:
            with opener.open(request, timeout=TIMEOUT_SECONDS) as response:
                if response.getcode() != 200:
                    fail("HTTP_RESPONSE", "정상적인 HTTP 200 응답이 아닙니다.")
                raw = response.read(MAX_RESPONSE_BYTES + 1)
                if len(raw) > MAX_RESPONSE_BYTES:
                    fail("RESPONSE_TOO_LARGE", "기상청 응답이 예상 크기를 초과했습니다.")
                expected = response.headers.get("Content-Length")
                if expected and (not expected.isdigit() or int(expected) != len(raw)):
                    raise http.client.IncompleteRead(raw)
            return raw, utc_now()
        except urllib.error.HTTPError as exc:
            status = exc.code
            exc.close()
            messages = {
                401: "기상청 인증 실패입니다. 현재 인증키의 유효성을 확인해야 합니다.",
                403: "API 이용 권한을 확인해야 합니다. 기상청 공식 안내상 활용 미신청 시 발생할 수 있습니다.",
                404: "기상청에서 요청 API 주소를 찾지 못했습니다.",
                429: "기상청 API 호출이 제한되었습니다.",
            }
            error = LookupFailure(f"HTTP_{status}", messages.get(status, "기상청 서버가 HTTP 오류를 반환했습니다."), status in {408, 429, 500, 502, 503, 504})
        except (TimeoutError, http.client.IncompleteRead, ConnectionError) as exc:
            code = "TIMEOUT" if isinstance(exc, TimeoutError) else "CONNECTION_INTERRUPTED"
            error = LookupFailure(code, "제한 시간 안에 기상청 응답을 끝까지 받지 못했습니다.", True)
        except urllib.error.URLError as exc:
            if isinstance(exc.reason, ssl.SSLError):
                error = LookupFailure("TLS_ERROR", "기상청 HTTPS 인증서 또는 보안 연결을 확인해야 합니다.")
            else:
                code = "TIMEOUT" if isinstance(exc.reason, TimeoutError) else "NETWORK_ERROR"
                error = LookupFailure(code, "GitHub 실행 서버에서 기상청에 정상 연결하지 못했습니다.", True)
        print(f"KMA attempt failed: {error.code}", flush=True)
        if not error.retryable or attempt == MAX_ATTEMPTS:
            raise error
        sleeper(5 * attempt)
    raise AssertionError("unreachable")


def parse_payload(raw):
    text = None
    for encoding in ("utf-8-sig", "cp949"):
        try:
            text = raw.decode(encoding)
            break
        except UnicodeDecodeError:
            pass
    if text is None:
        fail("ENCODING", "응답 문자인코딩을 확인할 수 없습니다.")
    try:
        payload = json.loads(text)
    except (ValueError, RecursionError):
        fail("RESPONSE_FORMAT", "JSON으로 요청했지만 올바른 JSON 응답을 받지 못했습니다.")

    candidates, totals = [], []

    def visit(node, depth=0):
        if depth > 10:
            fail("RESPONSE_SCHEMA", "응답 구조가 예상 범위를 벗어났습니다.")
        if isinstance(node, list):
            if not node:
                return
            if not all(isinstance(row, dict) for row in node):
                fail("RESPONSE_SCHEMA", "특보 목록의 자료 형식을 확인할 수 없습니다.")
            rows = [{str(k).strip().upper(): v for k, v in row.items()} for row in node]
            if not all(REQUIRED <= set(row) for row in rows):
                fail("RESPONSE_SCHEMA", "특보 목록에 필수 항목이 누락되어 있습니다.")
            candidates.append(rows)
            return
        if not isinstance(node, dict):
            fail("RESPONSE_SCHEMA", "특보 목록을 찾을 수 없습니다.")
        data = {str(k).strip().upper(): v for k, v in node.items()}
        for name in ("ERROR", "ERRORS"):
            if data.get(name):
                fail("API_ERROR", "기상청이 오류 응답을 반환했습니다.")
        for name in ("CODE", "RESULTCODE", "STATUSCODE"):
            if name in data and str(data[name]).strip() not in {"0", "00", "200", "OK", "SUCCESS"}:
                fail("API_ERROR", "기상청이 정상 처리가 아닌 응답 코드를 반환했습니다.")
        if data.get("SUCCESS") is False or str(data.get("STATUS", "")).lower() in {"error", "fail", "failed", "failure"}:
            fail("API_ERROR", "기상청 응답의 성공 상태를 확인할 수 없습니다.")
        status_code = str(data.get("STATUS", "")).strip()
        if status_code.isdigit() and status_code not in {"0", "00", "200"}:
            fail("API_ERROR", "기상청이 정상 처리가 아닌 상태 코드를 반환했습니다.")
        if any(name in data for name in ("PAGINATION", "PAGING", "PAGEINFO", "PAGE_INFO")):
            fail("PARTIAL_RESPONSE", "전체 현황인지 검증할 수 없는 페이지 정보가 반환되었습니다.")
        for name in ("TOTALCOUNT", "TOTAL_COUNT"):
            if name in data:
                if not str(data[name]).isdigit():
                    fail("PARTIAL_RESPONSE", "전체 자료 건수를 확인할 수 없습니다.")
                totals.append(int(data[name]))
        for name in ("PAGENO", "PAGE_NO", "PAGE"):
            if name in data and str(data[name]) != "1":
                fail("PARTIAL_RESPONSE", "전체 현황이 아닌 일부 페이지가 반환되었습니다.")
        if any(name in data for name in ("NUMOFROWS", "PAGE_SIZE", "PAGESIZE")) and not any(name in data for name in ("TOTALCOUNT", "TOTAL_COUNT")):
            fail("PARTIAL_RESPONSE", "페이지 크기는 있지만 전체 건수를 확인할 수 없습니다.")
        if any(data.get(name) for name in ("HASNEXT", "HAS_NEXT", "HAS_MORE", "HASMORE", "NEXT", "NEXT_PAGE", "NEXTPAGE", "NEXT_CURSOR")):
            fail("PARTIAL_RESPONSE", "전체 기상특보 현황을 받지 못했습니다.")
        if REQUIRED <= set(data):
            candidates.append([data])
            return
        for name in ("HEADER", "META", "METADATA"):
            if isinstance(data.get(name), dict):
                visit(data[name], depth + 1)
        for name in WRAPPERS:
            if name in data:
                visit(data[name], depth + 1)

    visit(payload)
    if not candidates:
        fail("EMPTY_OR_UNRECOGNIZED_RESPONSE", "빈 응답 또는 미확인 구조입니다. 특보 없음으로 판단하지 않습니다.")
    if len(candidates) != 1:
        fail("AMBIGUOUS_RESPONSE", "여러 특보 목록이 반환되어 현재 전체 현황을 특정할 수 없습니다.")
    rows = candidates[0]
    if totals and any(total != len(rows) for total in totals):
        fail("PARTIAL_RESPONSE", "전체 건수와 수신한 특보 건수가 일치하지 않습니다.")
    return rows


def scalar(value):
    if value is None or isinstance(value, (bool, list, dict)):
        fail("RESPONSE_SCHEMA", "특보 항목에 잘못된 값이 있습니다.")
    return str(value).strip()


def kst_time(value):
    value = scalar(value)
    if not re.fullmatch(r"\d{12}", value):
        fail("RESPONSE_TIME", "특보 시각 형식을 확인할 수 없습니다.")
    try:
        return datetime.strptime(value, "%Y%m%d%H%M").replace(tzinfo=KST)
    except ValueError:
        fail("RESPONSE_TIME", "잘못된 특보 시각이 포함되어 있습니다.")


def classify(rows, now):
    current = set()
    target_rows = 0
    preliminary = False
    for row in rows:
        region = scalar(row["REG_ID"])
        name = scalar(row["REG_KO"])
        if not re.fullmatch(r"[A-Z][A-Z0-9]{7}", region):
            fail("REGION_SCHEMA", "특보구역 코드 형식을 확인할 수 없습니다.")
        if (name in REGION_NAMES) != (region == REGION_ID):
            fail("REGION_MISMATCH", "전주 특보구역 코드와 명칭이 일치하지 않습니다.")
        wrn = scalar(row["WRN"])
        wrn = WARNING_TYPES.get(wrn, wrn)
        level = scalar(row["LVL"])
        level = LEVELS.get(level, level)
        command = scalar(row["CMD"])
        command = COMMANDS.get(command, command)
        if wrn not in set(WARNING_TYPES.values()) or level not in {"1", "2", "3"} or command not in ACTIVE_COMMANDS | CANCEL_COMMANDS:
            fail("WARNING_CODE", "문서에서 확인되지 않은 특보 코드가 반환되었습니다.")
        announced = kst_time(row["TM_FC"])
        effective = kst_time(row["TM_EF"])
        if announced > now + timedelta(minutes=2):
            fail("FUTURE_PUBLICATION", "조회시각 이후의 특보 발표 자료가 포함되어 있습니다.")
        if region != REGION_ID or wrn != "C":
            continue
        target_rows += 1
        if level == "1":
            preliminary = preliminary or command in ACTIVE_COMMANDS
            continue
        if effective > now:
            continue
        state = "cancelled" if command in CANCEL_COMMANDS else ("advisory" if level == "2" else "warning")
        current.add(state)
    if len(current) > 1:
        fail("CONFLICTING_WARNINGS", "전주 한파특보 현황에 서로 다른 상태가 함께 있어 확인이 필요합니다.")
    status = next(iter(current), "none")
    if status == "cancelled":
        status = "none"
    return status, {"recordCount": len(rows), "targetRecordCount": target_rows, "preliminary": preliminary}


def write_result(result, output=Path("weather.json")):
    temp = output.with_suffix(".json.tmp")
    temp.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temp.replace(output)


def report(result):
    print(f"KMA RESULT: {result['status']} - {LABELS[result['status']]}", flush=True)
    if result["status"] == "unknown":
        print(f"::warning title=KMA lookup failed::{result['errorCode']}: {result['errorMessage']}", flush=True)
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        lines = ["## 전주시 한파특보 조회", "", "- 결과: " + LABELS[result["status"]], "- 기준: 전주시 완산구 삼천동 / 전주 특보구역 / 발효시간"]
        if result["status"] == "unknown":
            lines += ["- 오류: " + result["errorCode"], "- 안내: " + result["errorMessage"], "- 현재 기상특보를 확인하지 못했습니다."]
        else:
            lines += ["- 확인시각(UTC): " + result["checkedAt"], "- 정상 판독 자료: " + str(result["recordCount"]) + "건"]
        with open(summary, "a", encoding="utf-8") as stream:
            stream.write("\n".join(lines) + "\n")


def main():
    result = {
        "status": "unknown", "checkedAt": "", "attemptedAt": utc_now().isoformat(),
        "source": "KMA", "region": "전주", "regionCode": REGION_ID,
        "location": "전북특별자치도 전주시 완산구 삼천동", "basis": "effective",
    }
    try:
        key = os.environ.get("KMA_API_KEY", "").strip()
        if not key or "authKey=" in key or "://" in key or any(c.isspace() for c in key):
            fail("KEY_CONFIGURATION", "GitHub의 KMA_API_KEY에는 인증키 문자열만 등록해야 합니다.")
        raw, received_at = fetch_document(key)
        rows = parse_payload(raw)
        status, details = classify(rows, received_at)
        result.update(status=status, checkedAt=received_at.isoformat(), **details)
    except LookupFailure as exc:
        result.update(errorCode=exc.code, errorMessage=exc.message)
    except Exception:
        # 예외 문자열에는 요청 URL이나 인증키가 섞일 수 있어 출력하지 않습니다.
        result.update(errorCode="UNEXPECTED_ERROR", errorMessage="예상하지 못한 조회 오류가 발생했습니다. 담당자 확인이 필요합니다.")
    write_result(result)
    report(result)


if __name__ == "__main__":
    main()
