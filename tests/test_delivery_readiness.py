import datetime as dt
import json
from concurrent.futures import ThreadPoolExecutor
import openpyxl
import pytest
from koushi import application as A, master as MW, validation as V
from koushi.registry import DistanceTable
from koushi.engines import collection
from tests.test_submission_checks import _fields
from tests.test_application_generate import _clean_exam, _save_master

@pytest.mark.parametrize('distance', ['-5','NaN','Infinity','-Infinity'])
def test_distance_invalid(tmp_path,distance):
    p=tmp_path/'distance.csv';p.write_text('服務單位,單程公里\n大學,'+distance,encoding='utf-8')
    with pytest.raises(ValueError): DistanceTable.load(p)

def test_distance_duplicate_and_ambiguity(tmp_path):
    p=tmp_path/'distance.csv';p.write_text('服務單位,單程公里\n大學,2\n大學,3',encoding='utf-8')
    with pytest.raises(ValueError): DistanceTable.load(p)
    with pytest.raises(ValueError): DistanceTable({'甲大學':2,'甲大學系所':3}).km_for('甲大學系所研究所')

def test_master_literal_external_text(tmp_path):
    path,m=_save_master(tmp_path,[_clean_exam('1141','A','=1+2',dt.date(2026,11,1))])
    wb=openpyxl.load_workbook(path)
    cell=wb['口試主表'].cell(2,15)
    assert cell.value=='=1+2' and cell.data_type=='s'

def test_duplicate_committees_rejected(tmp_path):
    f=_fields(committee_1_name='委員0',committee_2_name='委員0')
    with pytest.raises(ValueError,match='重複'): A.submit_application(student_id='A',fields=f,submissions_dir=tmp_path)
    assert not list(tmp_path.glob('*.json'))

def test_case_compare_and_set(tmp_path):
    A.submit_application(student_id='A',fields=_fields(),submissions_dir=tmp_path)
    f=_fields(base_version='1')
    def submit():
        try:return A.submit_application(student_id='A',fields=f,submissions_dir=tmp_path)['version']
        except ValueError:return 'conflict'
    with ThreadPoolExecutor(2) as pool:results=list(pool.map(lambda _:submit(),range(2)))
    assert sorted(map(str,results))==['2','conflict']
    assert json.loads((tmp_path/'1141-A.json').read_text(encoding='utf-8'))['version']==2
    assert len(list((tmp_path/'versions').glob('*.json')))==1

def test_source_issues_roundtrip(tmp_path):
    p,m=_save_master(tmp_path,[_clean_exam('1141','A','教授甲',dt.date(2026,11,1))])
    m.source_issues=[{'案件鍵':'1141-A','問題代碼':'COMMITTEE_MISMATCH','責任':'office','說明':'different'},
                     {'案件鍵':'','問題代碼':'ATTACHMENT_2_MISSING_STUDENT_ID','責任':'office','說明':'row 2'}]
    MW.save(m,m.source_issues,path=p)
    loaded=MW.load(p)
    assert {i['問題代碼'] for i in loaded.source_issues}=={'COMMITTEE_MISMATCH','ATTACHMENT_2_MISSING_STUDENT_ID'}
    assert any(i['問題代碼']=='COMMITTEE_MISMATCH' for i in A.review_queue(loaded,tmp_path/'state')[0].exceptions)

def test_collection_version_invalidates(tmp_path):
    p,m=_save_master(tmp_path,[_clean_exam('1141','A','教授甲',dt.date(2026,11,1))])
    old=collection.mark_collected(collection.create_items(m)[0],date=dt.date(2026,11,2))
    m.exams[0].委員[0].職稱='副教授'
    fresh=collection.reconcile(m,[old])[0]
    assert fresh.collected_date is None and fresh.content_version!=old.content_version
    assert fresh.history
