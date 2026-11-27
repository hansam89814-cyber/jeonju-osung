import json, os, urllib.request, urllib.parse
from datetime import datetime, timezone
from pathlib import Path
now = datetime.now(timezone.utc).isoformat()
result = {'status':'unknown','checkedAt':now,'source':'KMA','region':'전주'}
try:
    url='https://apihub.kma.go.kr/api/typ01/url/wrn_now_data_new.php?'+urllib.parse.urlencode({'fe':'e','tm':'','disp':'1','help':'0','authKey':os.environ['KMA_API_KEY']})
    with urllib.request.urlopen(url,timeout=25) as response:
        data=json.load(response)
    # Accept only a recognizable list of warning records; unknown schemas fail closed.
    def records(x):
        if isinstance(x,list) and x and all(isinstance(r,dict) for r in x):
            normalized=[{str(k).upper():v for k,v in r.items()} for r in x]
            if all({'REG_KO','WRN','LVL','CMD'} <= r.keys() for r in normalized): return normalized
        if isinstance(x,dict):
            for v in x.values():
                r=records(v)
                if r is not None:return r
        return None
    rows=records(data)
    if rows is None: raise ValueError('Unrecognized or empty response; verify official schema')
    levels=[]
    for r in rows:
        region=str(r['REG_KO']).strip(); wrn=str(r['WRN']).strip(); lvl=str(r['LVL']).strip(); cmd=str(r['CMD']).strip()
        if not (region=='전주' or region=='전주시' or region.endswith('(전주)')):continue
        if wrn not in ('C','한파'):continue
        if cmd not in ('1','발표','연장','변경','대치'): raise ValueError('Verify warning command')
        if lvl in ('2','경보'):levels.append('warning')
        elif lvl in ('1','주의보'):levels.append('advisory')
        else:raise ValueError('Verify warning level')
    # Absence of a matching event is deliberately not treated as a verified all-clear
    # until the live region code and API response format have been confirmed.
    result['status']='warning' if 'warning' in levels else 'advisory' if levels else 'unknown'
except Exception as e:
    print('Weather lookup needs verification:',type(e).__name__)
Path('weather.json').write_text(json.dumps(result,ensure_ascii=False)+'\n',encoding='utf-8')
