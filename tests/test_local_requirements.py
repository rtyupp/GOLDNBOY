import os, tempfile, unittest
from tests.helpers import cfg, FakeTG
from goldbot.app import BotApp
from goldbot.commands import build_handlers
from goldbot.notify.telegram import Telegram, CommandBot
from goldbot.providers.synthetic import SyntheticHistory
class TestLocalRequirements(unittest.TestCase):
    def test_no_external_ai_or_news_configuration(self):
        from goldbot.config import load_config
        c=load_config('config/config.yaml')
        self.assertIsNone(c.get('ai')); self.assertIsNone(c.get('news'))
        self.assertIn('smc_reclaim', c.get('strategies.enabled'))
        self.assertGreater(c.get('backtest.slippage'), 0)

    def test_smc_strategy_is_registered(self):
        from goldbot.strategies.impl import ALL
        self.assertIn('smc_reclaim', [cls.key for cls in ALL])
    def test_welcome_and_local_commands(self):
        c=cfg(paths__journal=os.path.join(tempfile.mkdtemp(),'j.sqlite'),paths__log_dir=tempfile.mkdtemp())
        app=BotApp(c,tg=FakeTG(),history_provider=SyntheticHistory())
        text=build_handlers(app)['/start']([])
        self.assertIn('ارحبو تراحيب المطر',text); self.assertIn('by: @QUOP9',text)
        self.assertNotIn('الأخبار',text); self.assertIn('محلي',build_handlers(app)['/status']([]))
    def test_reply_keyboard_is_bottom_keyboard(self):
        class S:
            def post(self,*a,**k):
                class R:
                    def json(self): return {'ok':True,'result':{}}
                self.kw=k; return R()
        s=S(); Telegram('token',None,[1],session=s).send_message(1,'hello')
        import json
        kb=json.loads(s.kw['data']['reply_markup'])
        self.assertIn('keyboard',kb); self.assertNotIn('inline_keyboard',kb)
        self.assertIn('🌐 نظرة السوق',[x['text'] for row in kb['keyboard'] for x in row])
if __name__=='__main__': unittest.main()
