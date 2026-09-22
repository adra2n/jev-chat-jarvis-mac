"""Offline regressions for the read → judgment pipeline. Run: python -B -m unittest discover -s tests -v.

Load the actual HUD methods through AST so the tests never start Cocoa, read the
screen, load user credentials, or make model calls. Perception uses synthetic OCR.

Issue #11's original set also covered the candidate-generation pipeline
(_reply_task / _reply_epoch / _pregen_loop / _stream_hook). That layer was
removed on purpose — everything here exercises what still exists: message
geometry, side classification, sender attachment, and the prejudge slot.
"""
import ast
import sys
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import Mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
from perception import TextBlock, extract_messages


def hud_harness():
    tree = ast.parse((ROOT / 'src/hud.py').read_text())
    source = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == 'HudController')
    names = {'_work_inner', '_push', '_reply_current', 'applyWaiting_',
             '_context_text', '_prejudge_loop'}
    methods = [n for n in source.body if isinstance(n, ast.FunctionDef) and n.name in names]
    assert methods, 'AST harness found no HudController methods to load'
    for method in methods:
        method.decorator_list = []
    klass = ast.ClassDef(name='Harness', bases=[], keywords=[], body=methods, decorator_list=[])
    scope = {'time': time, 'threading': threading, '_log': lambda *_: None,
             'screen_capture_ok': lambda: True, 'read_conversation': Mock(),
             'PALETTE': {'muted': None}, 'CONTEXT_TURNS': 8, 'JUDGE_TURNS': 4,
             'SLOW_TICK': 1, 'BURST_TICK': .45, 'FAST_TICK': .25, 'BURST_READS': 3,
             'SETTLE_S': 1.2, 'STABLE_READS': 3, 'EARLY_SETTLE_S': .7, 'MIN_GAP_S': 2}
    module = ast.fix_missing_locations(ast.Module(body=[klass], type_ignores=[]))
    exec(compile(module, str(ROOT / 'src/hud.py'), 'exec'), scope)
    return scope['Harness'], scope


Harness, HUD = hud_harness()


def block(text, x, y, w, h=.035):
    return TextBlock(text, 1.0, x, y, w, h)


class OutgoingTests(unittest.TestCase):
    def setUp(self):
        self.h = h = Harness()
        for name, value in dict(
            last_seen=None, analyzed_text=None, _win_wid=None, _fingerprint=None,
            _burst_left=3, _last_full=None, _last_nothem_sig=(), _show_boxes=False,
            _read_once=True, _last_skip_reason=None, _asked_permission=False,
            _prejudge_req=None, _prejudge_result=None,
            _prejudging=False, _paused=False, _analyzing=False,
            _stable_n=0, last_change_ts=0, last_analyze_ts=0,
            _prejudge_event=threading.Event(), _chat_title='',
        ).items():
            setattr(h, name, value)
        h._show = Mock()
        h._render = Mock()
        h.judge = Mock()
        h._model_lock = threading.Lock()
        h._judged_once = True
        for name in ['applyIncoming_', 'applyPending_', 'applyJudgment_',
                     'applyWaiting_', 'applyError_', 'applyPosition_',
                     'applyChat_', 'applyBoxes_']:
            setattr(h, name, Mock())
        self.queue = []
        h.performSelectorOnMainThread_withObject_waitUntilDone_ = lambda s, p, w: self.queue.append((s, p))

    def read(self, blocks, title='chat'):
        messages = extract_messages(blocks)
        HUD['read_conversation'].return_value = {
            'ok': True, 'unchanged': False, 'fingerprint': None,
            'window': {'wid': 1}, 'chat_title': title, 'messages': messages,
        }
        self.h._work_inner()
        return messages

    def flush(self):
        while self.queue:
            selector, payload = self.queue.pop(0)
            getattr(self.h, selector.replace(':', '_'))(payload)

    def incoming(self):
        self.read([block('下午开会', .40, .70, .15)])

    def clear_ui(self):
        # 先丢掉上一轮 read 排队、还没派发的 UI 更新，否则 flush() 会把
        # 上一次的 applyIncoming: 也算到这一轮头上。
        self.queue.clear()
        for name in ('applyIncoming_', 'applyPending_', 'applyJudgment_',
                     'applyWaiting_'):
            getattr(self.h, name).reset_mock()

    # --- 什么情况下「不能」起判断任务 ---------------------------------------

    def test_only_own_short_message_never_triggers_judgment(self):
        messages = self.read([block('11', .862, .284, .024, .024)])
        self.assertEqual(messages[0].side, 'me')
        self.assertIsNone(self.h._prejudge_req)
        self.assertFalse(self.h._prejudge_event.is_set())
        self.flush()
        self.h.applyWaiting_.assert_called_once_with(None)
        self.h.applyIncoming_.assert_not_called()
        self.h.applyJudgment_.assert_not_called()

    def test_ambiguous_wide_message_is_not_incoming(self):
        messages = self.read([block('宽文本横跨左右分界', .38, .70, .55)])
        self.assertEqual(messages[0].side, 'unknown')
        self.assertIsNone(self.h._prejudge_req)
        self.assertFalse(self.h._prejudge_event.is_set())

    def test_shifted_outgoing_continuation_stays_with_bubble(self):
        messages = self.read([block('自己长消息第一行', .55, .70, .29),
                              block('较短续行', .535, .66, .12)])
        self.assertEqual(len(messages), 1)
        self.assertEqual(messages[0].side, 'me')
        self.assertAlmostEqual(messages[0].x + messages[0].w, .84)
        self.assertIsNone(self.h._prejudge_req)

    def test_cropped_central_continuation_is_unknown(self):
        messages = self.read([block('只剩续行', .535, .66, .12)])
        self.assertEqual(messages[0].side, 'unknown')
        self.assertIsNone(self.h._prejudge_req)

    def test_later_wide_line_can_resolve_initial_unknown(self):
        messages = self.read([block('短首行', .535, .70, .12),
                              block('后面更长的一行', .55, .66, .29),
                              block('第三行', .54, .62, .13)])
        self.assertEqual(len(messages), 1)
        self.assertEqual(messages[0].side, 'me')
        self.assertEqual(len(messages[0].lines), 3)
        self.assertIsNone(self.h._prejudge_req)

    def test_no_incoming_shows_waiting_and_queues_no_new_work(self):
        self.incoming()
        self.assertIsNotNone(self.h._prejudge_req)
        req = self.h._prejudge_req
        self.clear_ui()
        self.read([block('自己回复', .80, .50, .10)])
        self.flush()
        for name in ('applyIncoming_', 'applyPending_', 'applyJudgment_'):
            getattr(self.h, name).assert_not_called()
        self.h.applyWaiting_.assert_called_once_with(None)
        # 我方消息不能凭空产生新的判断任务
        self.assertEqual(self.h._prejudge_req, req)
        self.assertIsNone(self.h.analyzed_text)

    def test_empty_ocr_shows_waiting_and_queues_nothing(self):
        self.incoming()
        self.clear_ui()
        self.read([])
        self.flush()
        self.h.applyWaiting_.assert_called_once_with(None)
        for name in ('applyIncoming_', 'applyPending_', 'applyJudgment_'):
            getattr(self.h, name).assert_not_called()

    # --- 消息几何：气泡归属与发送者名 ---------------------------------------

    def test_opposite_known_sides_never_fold_despite_close_edges(self):
        messages = extract_messages([block('收到的消息', .495, .70, .18),
                                     block('自己发出的消息', .51, .66, .34)])
        self.assertEqual([m.side for m in messages], ['them', 'me'])

    def test_incoming_wrapped_message_and_sender_preserved(self):
        messages = extract_messages([block('小王', .40, .80, .05, .020),
                                     block('第一行正文', .40, .65, .25),
                                     block('续行正文', .405, .61, .12)])
        self.assertEqual(len(messages), 1)
        self.assertEqual(messages[0].side, 'them')
        self.assertEqual(messages[0].sender, '小王')
        self.assertEqual(len(messages[0].lines), 2)

    def test_lone_short_incoming_line_is_kept_not_eaten(self):
        # 一行气泡实测 h=0.018~0.025，与发送者名 0.019~0.023 完全重叠——
        # 不能因为「短 + them + 孤行」就当成发送者名丢掉，否则短消息全军覆没。
        messages = extract_messages([block('200卖了一多半', .40, .70, .12, .018)])
        self.assertEqual(len(messages), 1)
        self.assertEqual(messages[0].side, 'them')
        self.assertIsNone(messages[0].sender)

    # --- 真的来了对方消息时 ---------------------------------------------------

    def test_real_incoming_still_triggers_judgment_and_keeps_own_context(self):
        self.read([block('下午开会', .40, .70, .15), block('我会带材料', .78, .50, .10)])
        self.assertEqual(self.h._prejudge_req[0], '下午开会')
        self.assertIn('我: 我会带材料', self.h._prejudge_req[1])
        self.flush()
        self.h.applyIncoming_.assert_called_once()

    # --- 迟到 / 跨会话不能污染当前状态 ---------------------------------------

    def test_same_text_in_different_chat_retriggers(self):
        self.incoming()
        self.h._prejudge_req = None      # 模拟这一句的预判已经被消费
        self.h.analyzed_text = '下午开会'  # 这个会话里已经分析过这句
        self.read([block('下午开会', .40, .70, .15)], title='另一个会话')
        self.assertIsNotNone(self.h._prejudge_req)  # 新会话必须重新起判
        self.assertIsNone(self.h.analyzed_text)     # 新会话必须允许重新分析

    def test_prejudge_completion_cannot_repopulate_cleared_state(self):
        self.incoming()
        class Finished(BaseException):
            pass
        self.h._prejudge_event = Mock()
        self.h._prejudge_event.wait.side_effect = [None, Finished()]
        def judge_then_clear(*args, **kwargs):
            self.read([])
            return {'intent': '约会议', 'confidence': 1, 'risk': 1}
        self.h.judge.judge.side_effect = judge_then_clear
        with self.assertRaises(Finished):
            self.h._prejudge_loop()
        self.assertIsNone(self.h._prejudge_result)


if __name__ == '__main__':
    unittest.main()
