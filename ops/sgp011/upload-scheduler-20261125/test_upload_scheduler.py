"""Batch integration and admission/replay independence; no provider requests."""
import ast,asyncio,contextlib,copy,hashlib,io,json,math,re,time,types,unittest
from pathlib import Path
from typing import Dict,Any,List,Optional
from unittest.mock import AsyncMock,Mock

from src.services.upload_exit_backoff import UploadExitBackoff

ERROR='Project-scoped image upload failed via /flow/uploadImage; cause=network_timeout; attempts=1'
DIGEST='a'*64
OTHER='b'*64

def failed_ref():
    return {'status':'failed','cause':'network_timeout','rpc_trace':{
        'auth_mode':'google_cookie_rpc','rpc_method':'maseQ','dispatch_started':True,
        'phase':'rpc_dispatch','success':False,'code':'rpc_transport_unavailable_curl_28',
        'submission_outcome':'unknown','transport':{'curl_code':28,'response_complete':False,'duration_ms':120031}}}

def success_ref():
    return {'status':'uploaded','rpc_trace':{'rpc_method':'maseQ','success':True,
        'submission_outcome':'rpc_accepted','transport':{'response_complete':True}}}

def handler():
    tree=ast.parse(Path('/app/src/services/generation_handler.py').read_text())
    cls=next(n for n in tree.body if isinstance(n,ast.ClassDef)and n.name=='GenerationHandler')
    names={'_upload_exit_backoff','_upload_transport_exclusions','_reference_upload_exclusions',
           '_upload_scheduler_timeout','_observe_upload_schedule','_upload_reference_images',
           '_select_image_recovery_account','_upload_transport_retry_reason'}
    methods=[n for n in cls.body if getattr(n,'name','')in names]
    admission=next(n for n in tree.body if isinstance(n,ast.ClassDef)and n.name=='_ReferenceUploadAdmission')
    routes={64:{'assigned_exit_sha256':DIGEST},65:{'assigned_exit_sha256':DIGEST},66:{'assigned_exit_sha256':OTHER}}
    ns={'asyncio':asyncio,'time':time,'json':json,'math':math,'re':re,'Optional':Optional,'Dict':Dict,
        'Any':Any,'List':List,'UploadExitBackoff':UploadExitBackoff,'upload_egress':types.SimpleNamespace(routes=lambda:routes),
        'deque':__import__('collections').deque}
    module=ast.Module(body=[admission,ast.ClassDef(name='Handler',bases=[],keywords=[],body=methods,decorator_list=[])],type_ignores=[])
    exec(compile(ast.fix_missing_locations(module),'<exact-candidate-methods>','exec'),ns)
    h=ns['Handler']();h._upload_exit_breaker=UploadExitBackoff();h._upload_batch_settle_reason=lambda *args:None
    return h,ns,routes

class Clock:
    value=1000.0
    def __call__(self):return self.value

class StateTests(unittest.TestCase):
    def setUp(self):self.c=Clock();self.b=UploadExitBackoff(self.c)
    def test_account_and_shared_exit_first_pause_expires(self):
        r=self.b.observe_timeout(64,DIGEST)
        self.assertEqual(self.b.scheduler_active(),{('account',64),('exit',DIGEST)})
        self.assertTrue(all(d['seconds_remaining']==120 for d in r['decisions']))
        self.assertFalse(r['authorizes_replay']);self.c.value+=120
        self.assertEqual(self.b.scheduler_active(),set())
    def test_repeat_uses_300s_and_parallel_failure_does_not_extend_forever(self):
        self.b.observe_timeout(64,DIGEST);self.c.value+=10;self.b.observe_timeout(64,DIGEST)
        until=self.b.observations[('account',64)]['until'];self.assertEqual(until,1300)
        self.c.value=1299;self.b.observe_timeout(64,DIGEST)
        self.assertEqual(self.b.observations[('account',64)]['until'],until)
        self.c.value=1300;self.assertEqual(self.b.scheduler_active(),set())
        self.c.value=1301;self.b.observe_timeout(64,DIGEST)
        self.assertEqual(self.b.observations[('account',64)]['until'],1601)
    def test_history_expires_and_is_memory_bounded(self):
        self.b.observe_timeout(64,DIGEST);self.c.value+=900
        r=self.b.observe_timeout(64,DIGEST);self.assertEqual(r['decisions'][0]['failures_in_window'],1)
        for i in range(600):self.b.observe_timeout(i+1,hashlib.sha256(str(i).encode()).hexdigest())
        self.assertLessEqual(len(self.b.observations),512)
    def test_no_registry_still_pauses_account_and_malformed_identity_not_shared(self):
        self.b.observe_timeout(64,None);self.assertEqual(self.b.scheduler_active(),{('account',64)})
        self.assertEqual(self.b.observe_timeout('64',DIGEST)['decisions'],[])
    def test_old_success_cannot_clear_new_failure(self):
        self.b.observe_timeout(64,DIGEST)
        self.assertEqual(self.b.observe_success(64,DIGEST,999),[])
        self.assertEqual(self.b.observe_success(64,DIGEST,1000),[])
        self.assertEqual(set(self.b.observe_success(64,DIGEST,1001)),{'account','exit'})
        self.assertEqual(self.b.scheduler_active(),set())
        self.c.value=1010;self.b.observe_timeout(64,DIGEST)
        self.assertEqual(self.b.observations[('account',64)]['failures'],2)
        self.assertEqual(self.b.observations[('account',64)]['until'],1310)
    def test_distinct_exit_and_legacy_policy_are_independent(self):
        self.b.observe_timeout(64,DIGEST);self.b.observe_timeout(66,OTHER)
        self.b.observe_success(64,DIGEST,1001)
        self.assertEqual(self.b.scheduler_active(),{('account',66),('exit',OTHER)})
        self.assertEqual(self.b.failed(DIGEST)['seconds'],120)
        self.assertEqual(self.b.failed(DIGEST)['seconds'],300)
        self.assertEqual(self.b.active(),{DIGEST})

class EvidenceTests(unittest.TestCase):
    def setUp(self):self.h,self.ns,self.routes=handler()
    def trace(self):return {'admission':{'status':'admitted'},'images':[failed_ref()]}
    def test_real_unknown_upload_evidence_grants_only_new_admission_backoff(self):
        trace=self.trace();error=RuntimeError(ERROR);error.outcome_unknown=True;error.submission_uncertain=True
        with contextlib.redirect_stdout(io.StringIO()):self.h._observe_upload_schedule(64,DIGEST,0,trace,error)
        self.assertEqual(self.h._reference_upload_exclusions([b'image']),{64,65})
        self.assertEqual(self.h._reference_upload_exclusions([]),set())
        self.assertTrue(error.outcome_unknown and error.submission_uncertain)
        self.assertIsNone(self.h._upload_transport_retry_reason(error,{'image_generation':{'reference_upload':trace}}))
        self.assertFalse(trace['scheduler_backoff']['authorizes_replay'])
    def test_wrong_rpc_phase_status_code_or_missing_transport_does_not_pause(self):
        mutations=[lambda t:t['admission'].update(status='timeout'),
          lambda t:t['images'][0].update(cause='CancelledError'),
          lambda t:t['images'][0]['rpc_trace'].update(rpc_method='ogiZ0b'),
          lambda t:t['images'][0]['rpc_trace'].update(auth_mode='legacy'),
          lambda t:t['images'][0]['rpc_trace'].update(phase='bootstrap'),
          lambda t:t['images'][0]['rpc_trace'].update(http_status=400),
          lambda t:t['images'][0]['rpc_trace'].update(submission_outcome='explicit_provider_rejection'),
          lambda t:t['images'][0]['rpc_trace'].update(transport={}),
          lambda t:t['images'][0]['rpc_trace']['transport'].update(duration_ms='unknown'),
          lambda t:t['images'][0]['rpc_trace']['transport'].update(duration_ms=float('nan')),
          lambda t:t['images'][0]['rpc_trace']['transport'].update(response_complete=True)]
        for change in mutations:
            t=self.trace();change(t);self.assertFalse(self.h._upload_scheduler_timeout(RuntimeError(ERROR),t))
        for e in [RuntimeError('content_policy_violation'),RuntimeError('image_upsample_failed'),asyncio.CancelledError()]:
            self.assertFalse(self.h._upload_scheduler_timeout(e,self.trace()))
    def test_success_requires_whole_valid_batch_and_new_start(self):
        b=self.h._upload_exit_backoff();b.observe_timeout(64,DIGEST)
        self.h._observe_upload_schedule(64,DIGEST,time.monotonic()+1,{'images':[success_ref(),failed_ref()]})
        self.assertIn(64,self.h._reference_upload_exclusions([b'i']))
        self.h._observe_upload_schedule(64,DIGEST,time.monotonic()-5,{'images':[success_ref()]})
        self.assertIn(64,self.h._reference_upload_exclusions([b'i']))
        with contextlib.redirect_stdout(io.StringIO()):self.h._observe_upload_schedule(64,DIGEST,time.monotonic()+1,{'images':[success_ref()]})
        self.assertEqual(self.h._reference_upload_exclusions([b'i']),set())
    def test_captured_actual_id64_timeout_and_success_shapes(self):
        rows=json.loads(Path('/tests/real-traces.json').read_text())['rows']
        failure=next(r for r in rows if any(ref.get('status')=='failed' and ref.get('cause')=='network_timeout' for ref in r['refs']))
        trace={'admission':failure['upload_admission'],'images':failure['refs']}
        self.assertTrue(self.h._upload_scheduler_timeout(RuntimeError(ERROR),trace))
        with contextlib.redirect_stdout(io.StringIO()):self.h._observe_upload_schedule(64,DIGEST,0,trace,RuntimeError(ERROR))
        self.assertEqual(self.h._reference_upload_exclusions([b'i']),{64,65})

    def test_original_retry_and_content_classification_functions_unchanged(self):
        before=ast.parse(Path('/tests/generation_handler.before.py').read_text())
        after=ast.parse(Path('/app/src/services/generation_handler.py').read_text())
        names={'_upload_transport_retry_reason','_cross_account_retry_reason','_upsample_regeneration_decision','_classify_public_image_error'}
        def chosen(t):return {n.name:ast.dump(n)for n in ast.walk(t)if isinstance(n,(ast.FunctionDef,ast.AsyncFunctionDef))and n.name in names}
        self.assertEqual(chosen(before),chosen(after));self.assertEqual(len(chosen(after)),len(names))

class BatchTests(unittest.IsolatedAsyncioTestCase):
    async def test_parallel_failures_count_once_and_children_and_capacity_drained(self):
        h,ns,_=handler();token=types.SimpleNamespace(id=64,at='opaque')
        trace={'environment':{'assigned_exit_sha256':DIGEST},'reference_upload':{'images':[{},{}],'uploaded_count':0}}
        calls=[]
        async def upload(at,data,aspect,project_id,diagnostic_trace):
            calls.append(data);diagnostic_trace.update(failed_ref());await asyncio.sleep(0)
            e=RuntimeError(ERROR);e.outcome_unknown=True;raise e
        h.flow_client=types.SimpleNamespace(upload_image=upload)
        with contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaisesRegex(RuntimeError,'network_timeout'):
                await h._upload_reference_images(token,'project',{'aspect_ratio':'square'},[b'first',b'second'],trace)
        self.assertEqual(calls,[b'first',b'second'])
        self.assertEqual(h._upload_exit_backoff().observations[('account',64)]['failures'],1)
        self.assertEqual(h._reference_upload_exclusions([b'i']),{64,65})
        self.assertEqual(h._reference_upload_admission._active,{})
        self.assertEqual(trace['reference_upload']['uploaded_count'],0)
    async def test_success_order_and_cancellation_preserved(self):
        h,ns,_=handler();token=types.SimpleNamespace(id=64,at='opaque')
        trace={'reference_upload':{'images':[{},{}],'uploaded_count':0}}
        async def upload(at,data,aspect,project_id,diagnostic_trace):
            if data==b'first':await asyncio.sleep(.01)
            diagnostic_trace.update(success_ref());return data.decode()
        h.flow_client=types.SimpleNamespace(upload_image=upload)
        out=await h._upload_reference_images(token,'project',{'aspect_ratio':'square'},[b'first',b'second'],trace)
        self.assertEqual([r['name']for r in out],['first','second']);self.assertEqual(h._upload_exit_backoff().scheduler_active(),set())
        started=asyncio.Event()
        async def block(*a,**kw):started.set();await asyncio.sleep(30)
        h.flow_client.upload_image=block
        task=asyncio.create_task(h._upload_reference_images(token,'p',{'aspect_ratio':'square'},[b'i'],{'reference_upload':{'images':[{}],'uploaded_count':0}}))
        await started.wait();task.cancel()
        with self.assertRaises(asyncio.CancelledError):await task
        self.assertEqual(h._upload_exit_backoff().scheduler_active(),set());self.assertEqual(h._reference_upload_admission._active,{})
    async def test_recovery_selection_avoids_shared_exit_but_keeps_distinct_account(self):
        h,ns,_=handler();h._upload_exit_backoff().observe_timeout(64,DIGEST);calls=[]
        async def select_token(**kw):
            calls.append(kw);return next((x for x in [64,65,66]if x not in kw['excluded_token_ids']),None)
        h.load_balancer=types.SimpleNamespace(select_token=select_token);h._busy_reference_upload_accounts=lambda:set()
        out=await h._select_image_recovery_account(model='m',model_config={},model_quota_key='m',images=[b'i'],
            attempted_ids=set(),exit_exclusions=set(),preferred_upload_accounts=set(),failed_upload_exits=set(),retry_deadline=time.monotonic()+30,account_attempt={})
        self.assertEqual(out,66);self.assertEqual(calls[0]['excluded_token_ids'],{64,65})

if __name__=='__main__':unittest.main()
