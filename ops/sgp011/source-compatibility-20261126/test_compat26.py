import ast
import asyncio
import collections
import copy
import hashlib
import importlib.util
import json
from pathlib import Path
import runpy
import time
from types import SimpleNamespace as NS
import unittest
from unittest.mock import AsyncMock
from urllib.parse import urlparse

ROOT=Path(__file__).resolve().parent
SOURCE=Path('/app') if Path('/app/src/services/input_source_recovery.py').exists() else ROOT/'candidate'
recovery=runpy.run_path(str(SOURCE/'src/services/input_source_recovery.py'))
connection_recovery_allowed=recovery['connection_recovery_allowed']
source_outcome=recovery['source_outcome']
spec=importlib.util.spec_from_file_location('transfer_diag',ROOT/'baseline/src/services/image_diagnostics.py')
diag=importlib.util.module_from_spec(spec);spec.loader.exec_module(diag)


def attempt(**kw):
    return dict(stage='input_reference_download',success=False,curl_code=28,
                last_observed_phase='dns_completed',response_complete=False,**kw)


class Boundaries(unittest.TestCase):
    def test_two_connection_errors_only(self):
        self.assertTrue(connection_recovery_allowed([attempt(),attempt()],50))
        for rows in [[],[attempt()],[attempt(),attempt(),attempt()]]:
            self.assertFalse(connection_recovery_allowed(rows,50))
    def test_original_capture_shape(self):
        rows=[attempt(),attempt()]
        rows[0].update(duration_ms=8002.039,connect_timeout_ms=8000)
        rows[1].update(duration_ms=8003.597,alternate_candidates=4,
                       alternate_probes=[{'outcome':'failed'}]*4)
        self.assertTrue(connection_recovery_allowed(rows,68))
    def test_no_post_or_started_response_recovery(self):
        for change in [{'stage':'google_generation'},{'stage':'reference_upload'},
                       {'curl_code':92},{'http_status_code':403},{'http_status_code':200},
                       {'success':True},{'response_complete':True},
                       {'last_observed_phase':'response_bytes_received'},
                       {'last_observed_phase':'request_bytes_sent'},
                       {'uploaded_bytes':1},{'downloaded_bytes':1},{'first_byte_seconds':.001}]:
            row=attempt();row.update(change)
            with self.subTest(change=change):self.assertFalse(connection_recovery_allowed([attempt(),row],50))
    def test_deadline_and_bad_counters(self):
        for value in [12.9,float('nan'),float('inf'),True,None]:
            self.assertFalse(connection_recovery_allowed([attempt(),attempt()],value))
        for value in [float('nan'),-1,'0',True]:
            a=attempt();a['uploaded_bytes']=value
            self.assertFalse(connection_recovery_allowed([a,attempt()],50))
    def test_trace_has_no_input_payload(self):
        out={};source_outcome(out,[attempt()],True,False)
        self.assertEqual(out,dict(success=False,attempts=1,retry_safe=True,
                         stage='input_reference_download',execution_outcome='not_started'))
    def test_non_target_sources_byte_exact(self):
        for name in ['cookie_auth','cookie_media','flow_cookie_rpc','generation_handler','upload_exit_backoff']:
            baseline=ROOT/'baseline/src/services'/f'{name}.py'
            actual=SOURCE/'src/services'/f'{name}.py'
            if actual.exists():self.assertEqual(actual.read_bytes(),baseline.read_bytes())
    def test_selection_and_capacity_code_unchanged(self):
        old=ast.parse((ROOT/'baseline/src/services/load_balancer.py').read_text())
        new=ast.parse((SOURCE/'src/services/load_balancer.py').read_text())
        def methods(tree):
            cls=next(n for n in tree.body if isinstance(n,ast.ClassDef))
            return{n.name:ast.dump(n,include_attributes=False)for n in cls.body if isinstance(n,(ast.FunctionDef,ast.AsyncFunctionDef))}
        a,b=methods(old),methods(new)
        self.assertEqual({k:v for k,v in a.items()if k!='get_unavailable_diagnostic'},
                         {k:v for k,v in b.items()if k!='get_unavailable_diagnostic'})


class HTTPException(Exception):
    def __init__(self,status_code,detail):self.status_code=status_code;self.detail=detail


class Failure(Exception):
    def __init__(self,code=28):
        self.code=code;self.response=NS(infos={0x300004:.001},status_code=None)


def response(status=200,content=b'original-bytes'):
    return NS(status_code=status,content=content,headers={'Content-Length':str(len(content))},infos={})


class Loader(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.plan=[];self.requests=[];self.sessions=[];self.alt=AsyncMock(return_value=({}, {'dns_route':'system'}))
        test=self
        class Session:
            def __init__(self,**kw):test.sessions.append(kw)
            async def __aenter__(self):return self
            async def __aexit__(self,*_):pass
            async def get(self,url,**kw):
                test.requests.append((url,kw));result=test.plan.pop(0)
                if isinstance(result,BaseException):raise result
                return result
        ns=dict(asyncio=asyncio,time=time,hashlib=hashlib,json=json,urlparse=urlparse,
                Optional=__import__('typing').Optional,HTTPException=HTTPException,
                connection_recovery_allowed=connection_recovery_allowed,source_outcome=source_outcome,
                AsyncSession=Session,alternate_source_transport=self.alt,
                CurlOpt=NS(CONNECTTIMEOUT_MS=156),CurlECode=NS(HTTP2=92),CurlHttpVersion=NS(V1_1=2),
                CURL_INFOS=diag.CURL_INFOS,begin_transfer=diag.begin_transfer,
                finish_transfer=diag.finish_transfer,emit_transfer_failure=lambda *_:None,
                curl_code=diag.curl_code,generation_handler=None,
                debug_logger=NS(log_warning=lambda *_:None))
        tree=ast.parse((SOURCE/'src/api/routes.py').read_text())
        fns=[n for n in tree.body if isinstance(n,ast.AsyncFunctionDef)and n.name in ['retrieve_image_data','_load_image_bytes_from_uri']]
        exec(compile(ast.Module(body=fns,type_ignores=[]),'<source-loader>','exec'),ns)
        self.retrieve=ns['retrieve_image_data'];self.load=ns['_load_image_bytes_from_uri']
    async def test_first_success_is_one_original_get(self):
        self.plan=[response()];self.assertEqual(await self.load('https://source.invalid/image'),b'original-bytes')
        self.assertEqual(len(self.requests),1);self.alt.assert_not_awaited()
        self.assertEqual(self.requests[0][1]['proxies'],{'http':'','https':''})
        self.assertFalse(self.sessions[0]['trust_env'])
    async def test_third_connection_only_get_can_recover(self):
        self.plan=[Failure(),Failure(),response()]
        self.assertEqual(await self.load('https://source.invalid/image'),b'original-bytes')
        self.assertEqual(len(self.requests),3);self.alt.assert_awaited_once()
        self.assertEqual([s['curl_options'][156]for s in self.sessions],[8000,8000,12000])
        self.assertTrue(all(url=='https://source.invalid/image'for url,_ in self.requests))
    async def test_exhausted_network_becomes_retry_safe_502(self):
        self.plan=[Failure(),Failure(),Failure()]
        with self.assertRaises(HTTPException)as e:await self.load('https://source.invalid/private?secret=DO_NOT_LOG')
        self.assertEqual(e.exception.status_code,502)
        self.assertEqual(e.exception.detail['code'],'reference_source_unavailable')
        self.assertTrue(e.exception.detail['retry_safe']);self.assertEqual(e.exception.detail['execution_outcome'],'not_started')
        self.assertEqual(e.exception.detail['attempts'],3);self.assertNotIn('DO_NOT_LOG',str(e.exception.detail))
    async def test_permanent_403_404_stop_and_stay_400(self):
        for status in [400,401,403,404]:
            self.plan=[response(status)];self.requests.clear()
            with self.assertRaises(HTTPException)as e:await self.load('https://source.invalid/image')
            self.assertEqual(e.exception.status_code,400);self.assertEqual(len(self.requests),1)
    async def test_transient_503_get_only_two_attempts(self):
        self.plan=[response(503),response(503)]
        with self.assertRaises(HTTPException)as e:await self.load('https://source.invalid/image')
        self.assertEqual(e.exception.status_code,502);self.assertEqual(len(self.requests),2)
    async def test_empty_or_incomplete_200_not_accepted(self):
        bad=response();bad.headers={'Content-Length':'999'}
        self.plan=[bad,bad]
        with self.assertRaises(HTTPException)as e:await self.load('https://source.invalid/image')
        self.assertEqual(e.exception.status_code,502);self.assertEqual(len(self.requests),2)
    async def test_output_asset_keeps_single_attempt(self):
        self.plan=[Failure()];self.assertIsNone(await self.retrieve('https://source.invalid/image','gemini_output_download'))
        self.assertEqual(len(self.requests),1);self.alt.assert_not_awaited()
    async def test_cancellation_never_retried(self):
        self.plan=[asyncio.CancelledError()]
        with self.assertRaises(asyncio.CancelledError):await self.load('https://source.invalid/image')
        self.assertEqual(len(self.requests),1)
    async def test_http2_uses_existing_http1_retry_not_extra_attempt(self):
        self.plan=[Failure(92),Failure(92)]
        with self.assertRaises(HTTPException):await self.load('https://source.invalid/image')
        self.assertEqual(len(self.requests),2);self.assertEqual(self.requests[1][1]['http_version'],2)


class PoolDiagnostic(unittest.IsolatedAsyncioTestCase):
    async def check(self,held):
        source=ast.parse((SOURCE/'src/services/load_balancer.py').read_text());cls=next(n for n in source.body if isinstance(n,ast.ClassDef));fn=next(n for n in cls.body if getattr(n,'name','')=='get_unavailable_diagnostic')
        ns=dict(Optional=__import__('typing').Optional,Counter=collections.Counter,
                ultra_reserved_for_4k_image=lambda *_a,**_kw:False,
                supports_model_for_tier=lambda *_:True,normalize_user_paygate_tier=lambda x:x)
        exec(compile(ast.Module(body=[fn],type_ignores=[]),'<pool-diag>','exec'),ns)
        tokens=[NS(id=n,user_paygate_tier='Ultra',image_enabled=True)for n in range(1,4)]
        rows=[dict(token_id=t.id,status='manual_required'if t.id in held else'verified',last_error='bootstrap_identity_mismatch'if t.id in held else'')for t in tokens]
        reader=AsyncMock(return_value=rows);obj=NS(token_manager=NS(get_active_tokens=AsyncMock(return_value=tokens),get_token_ids_in_model_cooldown=AsyncMock(return_value=set()),cookie_auth=NS(rows=reader)),_check_extension_route=AsyncMock(return_value=(True,None)))
        out=await ns['get_unavailable_diagnostic'](obj,model='test-4k',model_quota_key='GEM_PIX_2');reader.assert_awaited_once();return out
    async def test_all_identity_holds_get_precise_reason(self):
        r=await self.check({1,2,3});self.assertEqual(r['reason'],'identity_verification_required');self.assertFalse(r['session_diagnostic']['changes_admission'])
    async def test_mixed_candidates_not_falsely_all_held(self):
        r=await self.check({1,2});self.assertEqual(r['reason'],'session_invalid');self.assertEqual(r['session_diagnostic']['identity_hold_candidates'],2)


if __name__=='__main__':unittest.main()
