import argparse,json
from pathlib import Path
from .store import connect
from .source import read,file_record
from . import recipe,vat,shade,report,legacy_capture
HANDLERS={'recipe-import':recipe.import_data,'vat-plan':vat.import_data,'shade-import':shade.import_data,'register-import':legacy_capture.import_data}
def main():
    p=argparse.ArgumentParser(prog='dyebatch');p.add_argument('command',choices=[*HANDLERS,'demo','report','audit']);p.add_argument('--db',default='state.sqlite');p.add_argument('--input');a=p.parse_args();db=connect(a.db)
    if a.command=='report':print(json.dumps(report.build(db),ensure_ascii=False,indent=2));return
    if a.command=='audit':print(json.dumps(report.audit(db),ensure_ascii=False,indent=2));return
    if a.command=='demo':
        for command,name in [('recipe-import','recipe_ok.json'),('vat-plan','vat_ok.json'),('shade-import','shade_ok.json')]:
            data,raw=read(Path(__file__).resolve().parents[1]/'fixtures'/name)
            HANDLERS[command](db,data)
            with db:file_record(db,command,raw)
        print(json.dumps(report.build(db),ensure_ascii=False,indent=2));return
    if not a.input:p.error('--input required')
    data,raw=read(a.input)
    result=HANDLERS[a.command](db,data)
    with db:file_record(db,a.command,raw)
    print(json.dumps(result,ensure_ascii=False,indent=2))
if __name__=='__main__':main()

