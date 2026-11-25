import asyncio
from contextlib import asynccontextmanager
import unittest
from unittest.mock import patch

from src.services import input_source_dns as d

URL='https://oss.zjydai.com/private-path?sig=PRIVATE'

class Response:
    def __init__(self,status=206,chunks=(b'original',)):
        self.status_code=status;self.chunks=chunks
        self.infos={0x300004:.01,0x300005:.02,0x300021:.04,0x300011:.08}
    async def aiter_content(self):
        for chunk in self.chunks:yield chunk

class Session:
    response=Response()
    kwargs=[]
    def __init__(self,**kw):self.kwargs.append(kw)
    async def __aenter__(self):return self
    async def __aexit__(self,*args):pass
    @asynccontextmanager
    async def stream(self,*args,**kw):
        self.kwargs.append(kw)
        if isinstance(self.response,BaseException):raise self.response
        yield self.response

class ProbeTests(unittest.IsolatedAsyncioTestCase):
    async def test_original_tls_range_host_and_timings_preserved(self):
        diag={};Session.kwargs=[]
        with patch.object(d,'AsyncSession',Session),patch.object(Session,'response',Response()):
            self.assertEqual(await d.source_range_reachable('8.8.8.8',URL,diag),'8.8.8.8')
        self.assertEqual(diag['outcome'],'verified');self.assertEqual(diag['received_bytes'],8)
        self.assertEqual(diag['transport']['tls_seconds'],.04)
        self.assertNotIn('PRIVATE',str(diag));self.assertNotIn('8.8.8.8',str(diag))
        self.assertTrue(Session.kwargs[1]['verify']);self.assertFalse(Session.kwargs[1]['allow_redirects'])
        self.assertEqual(Session.kwargs[1]['timeout'],3)
    async def test_rejection_empty_and_tls_errors_have_diagnostics(self):
        for response,expected in [(Response(403),'http_rejected'),(Response(chunks=()),'empty_body'),(TimeoutError(),'failed')]:
            diag={}
            with patch.object(d,'AsyncSession',Session),patch.object(Session,'response',response):
                self.assertIsNone(await d.source_range_reachable('8.8.8.8',URL,diag))
            self.assertEqual(diag['outcome'],expected);self.assertFalse(diag['transport']['response_complete'])
    async def test_cancellation_propagates_and_is_recorded(self):
        diag={}
        with patch.object(d,'AsyncSession',Session),patch.object(Session,'response',asyncio.CancelledError()):
            with self.assertRaises(asyncio.CancelledError):await d.source_range_reachable('8.8.8.8',URL,diag)
        self.assertEqual(diag['outcome'],'cancelled')
    async def test_all_failed_probes_retain_original_resolution(self):
        async def resolver(endpoint):return ['8.8.8.8','1.1.1.1']
        async def probe(ip,url,diag):diag.update(outcome='failed',curl_code=28);return None
        with patch.object(d,'resolver_answers',resolver),patch.object(d,'source_range_reachable',probe):
            transport,diag=await d.alternate_source_transport(URL,90)
        self.assertEqual(transport,{});self.assertEqual(diag['dns_route'],'system')
        self.assertEqual(diag['alternate_candidates'],2)
        self.assertTrue(all(x['curl_code']==28 for x in diag['alternate_probes']))
    async def test_verified_candidate_only_and_losers_cancelled(self):
        cancelled=asyncio.Event()
        async def resolver(endpoint):return ['8.8.8.8','1.1.1.1']
        async def probe(ip,url,diag):
            if ip=='8.8.8.8':diag['outcome']='verified';return ip
            try:await asyncio.sleep(30)
            except asyncio.CancelledError:diag['outcome']='cancelled';cancelled.set();raise
        with patch.object(d,'resolver_answers',resolver),patch.object(d,'source_range_reachable',probe):
            transport,diag=await d.alternate_source_transport(URL,90)
        self.assertTrue(transport['verify']);self.assertTrue(diag['alternate_tls_verified'])
        self.assertTrue(cancelled.is_set());self.assertEqual(diag['alternate_probe_budget_ms'],3000)
    async def test_unapproved_host_or_low_budget_never_probes(self):
        async def fail(*a):raise AssertionError('unexpected network')
        with patch.object(d,'resolver_answers',fail):
            for url,budget in [('https://evil.test/p',90),(URL,7)]:
                self.assertEqual(await d.alternate_source_transport(url,budget),({}, {'dns_route':'system'}))
    def test_public_answer_chain_rejects_private_and_unrelated_answers(self):
        payload={'Status':0,'Answer':[{'name':d.SOURCE_HOST,'type':1,'data':'127.0.0.1'},
                 {'name':'evil.test','type':1,'data':'8.8.8.8'}, {'name':d.SOURCE_HOST,'type':1,'data':'1.1.1.1'}]}
        self.assertEqual(d.public_answers(payload),['1.1.1.1'])

if __name__=='__main__':unittest.main()
