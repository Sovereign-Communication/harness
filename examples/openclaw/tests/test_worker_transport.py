import unittest
from runtime.dogfood_worker import GeneratedDiff, MODEL, OneCandidateTransport

class TransportTests(unittest.TestCase):
    def test_records_reported_usage(self):
        usage={'prompt_tokens':17,'completion_tokens':9,'cost':0}
        wire=OneCandidateTransport(GeneratedDiff('patch',usage))
        _,response=wire.post('https://openrouter.ai/api/v1/chat/completions','unused',{'model':MODEL})
        self.assertEqual(response['usage'],usage)
    def test_missing_usage_is_not_fabricated(self):
        wire=OneCandidateTransport('patch')
        _,response=wire.post('https://openrouter.ai/api/v1/chat/completions','unused',{'model':MODEL})
        self.assertNotIn('prompt_tokens',response['usage'])
    def test_untrusted_native_hosts_refused(self):
        for url in ['https://api.typesafe.ai.attacker.invalid/v1/systemone','http://api.typesafe.ai/v1/systemone','https://attacker.invalid/typesafe.ai']:
            with self.subTest(url=url), self.assertRaises(RuntimeError):
                OneCandidateTransport('patch').post(url,'unused',{})
if __name__=='__main__':unittest.main()
