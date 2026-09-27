from __future__ import annotations

import json
import unittest
from pathlib import Path


class UiLocalizationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        root = Path(__file__).parents[1]
        cls.locale = json.loads((root / "locales" / "zh-CN.json").read_text(encoding="utf-8"))
        cls.html_source = (root / "product" / "server.py").read_text(encoding="utf-8")

    def test_status_mapping_does_not_change_internal_values(self):
        expected = {"READY": "已就绪", "PAUSED": "已暂停", "CONFIG_REQUIRED": "需要配置", "AVAILABLE": "可用", "BLOCKED": "已阻止", "UNAVAILABLE": "不可用"}
        for key, value in expected.items():
            self.assertEqual(self.locale["status"][key], value)
            self.assertNotEqual(key, value)

    def test_decision_display_mapping(self):
        self.assertEqual(self.locale["decision"]["ACCEPT"], "接受")
        self.assertEqual(self.locale["decision"]["REWORK"], "返工")
        self.assertEqual(self.locale["decision"]["COMPLETE_MEETING"], "结束会议")

    def test_product_shell_uses_zh_cn_resource(self):
        self.assertIn('lang="zh-CN"', self.html_source)
        self.assertIn("const I18N =", self.html_source)
        self.assertIn("localizeStatic", self.html_source)
        self.assertNotIn("localStorage", self.html_source)

    def test_credential_input_is_password_type(self):
        self.assertIn('id=\"brainCredential\" type=\"password\"', self.html_source)

    def test_experimental_panel_state_survives_refresh(self):
        self.assertIn("experimentalPanelExpanded", self.html_source)
        self.assertIn("readUiInteractionState", self.html_source)
        self.assertIn("restoreUiInteractionState", self.html_source)

    def test_desktop_brain_poc_button_visible_without_expanding_experimental(self):
        self.assertIn('>桌面主脑测试</button>', self.html_source)
        self.assertIn("<details class=\"experimental\">", self.html_source)
        experimental_block = self.html_source.split("<details class=\"experimental\">")[1].split("</details>", 1)[0]
        self.assertNotIn("runDesktopBrainPoc()", experimental_block)

    def test_polling_does_not_reset_ui_expansion_state(self):
        self.assertIn("const renderBeforeUiState=render;render=function(s)", self.html_source)
        self.assertIn("setInterval(()=>{if(selected)refresh()},2500)", self.html_source)

    def test_primary_brain_ui_does_not_show_internal_class_name(self):
        self.assertIn("主脑：GPT 网页版", self.html_source)
        self.assertIn("'ManualBrainBridge':I18N.brain.manual", self.html_source)

    def test_confirmed_english_labels_are_localized(self):
        for label in ("Members / Agents", "Join this Meeting", "REWORK instruction", "Legacy / Experimental"):
            self.assertIn(label, self.html_source)
            self.assertIn("I18N.task.members", self.html_source)
        self.assertIn("加入本次会议", self.html_source)
        self.assertIn("I18N.brain.reworkInstruction", self.html_source)

    def test_v101_product_brand_and_protocol_terms_render_in_chinese(self):
        from ai_meeting_room.product.server import UI_HTML

        self.assertIn("AI 会议 Room", UI_HTML)  # guarded by the runtime brand restoration map
        self.assertIn("AI Meeting Room", UI_HTML)
        self.assertIn("主脑决策数据包", UI_HTML)
        self.assertIn("主脑请求", UI_HTML)
        self.assertIn("手动主脑交接", UI_HTML)
        self.assertIn('s=s.replace("AI 会议 Room","AI Meeting Room")', UI_HTML)
        self.assertIn("phase3cRenderRuntime", UI_HTML)
        self.assertIn("<summary>技术详情</summary>", UI_HTML)
        self.assertIn("PRODUCT_SHELL_READY:'本地服务正在运行'", UI_HTML)
        self.assertIn("v101RuntimeWithoutLocale=phase3cRenderRuntime", UI_HTML)
        for mapping in (
            'replace("stop派发","停止派发")',
            'replace("Cloudflare","外部人机验证")',
            'replace("标准 JSON","标准格式")',
            'replace("会议总结 Markdown","会议总结")',
            'replace("Join this 会议","加入本次会议")',
            'replace("Legacy / Experimental: Send 主脑 Transport POC","旧版/实验功能：发送主脑传输测试")',
            'replace("Remove API 密钥","移除 API 密钥")',
            'replace("AgentHeartbeatReceived","智能体心跳更新")',
            'replace("会议Completed","会议已完成")',
            'replace("MEETING_SUMMARY_EXPORTED","已导出会议总结")',
            'replace("查看 Packet","查看数据包")',
            'replace("MiniMax Code","其他编程智能体")',
            'replace("其他智能体、GPT 网页自动化和 API 主脑均不阻塞 V1。","其他智能体、网页自动化实验和 API 主脑均不影响当前版本。")',
            'replace("健康状态：true","健康状态：正常")',
            'replace("Workbuddy","工作伙伴")',
            'replace("Zcode","智能代码助手")',
            'replace("Claude Code","克劳德代码助手")',
            'replace("Qwen","通义千问")',
            'replace("OpenAI 兼容接口","兼容接口")',
            'replace(" from ","，来源：")',
            'replace("MEETING 已暂停","会议已暂停")',
            'replace("派发 disabled until all health checks pass.","在所有健康检查通过之前，任务派发保持禁用。")',
        ):
            self.assertIn(mapping, UI_HTML)

    def test_v101_typography_and_responsive_layout_have_readable_minimums(self):
        from ai_meeting_room.product.server import UI_HTML

        self.assertIn("font-size:15px;line-height:1.5", UI_HTML)
        self.assertIn("min-height:40px", UI_HTML)
        self.assertIn("@media(max-width:720px)", UI_HTML)
        self.assertIn("overflow-wrap:anywhere", UI_HTML)

    def test_long_participant_value_and_health_code_use_separate_lines(self):
        from ai_meeting_room.product.server import UI_HTML

        self.assertIn('class="phase3c-cell-value ', UI_HTML)
        self.assertIn("phase3c-cell-code", UI_HTML)
        self.assertIn(".phase3c-cell-value>b,.phase3c-cell-code{display:block", UI_HTML)

    def test_v101_localizes_accessibility_and_api_setting_placeholders(self):
        from ai_meeting_room.product.server import UI_HTML

        self.assertIn('replace("Brain Packet","主脑数据包")', UI_HTML)
        self.assertIn("document.querySelectorAll('[aria-label],[title]')", UI_HTML)
        self.assertIn("请输入兼容接口地址", UI_HTML)
        self.assertIn("value==='model'?'模型名称'", UI_HTML)

    def test_browser_challenge_label_does_not_duplicate_translated_words(self):
        from ai_meeting_room.product.server import _V101_UI_TEXT_OVERRIDES

        self.assertIn(
            ("当前被外部 Cloudflare 验证挑战阻断", "当前被外部人机验证挑战阻断"),
            _V101_UI_TEXT_OVERRIDES,
        )

    def test_v101_localizes_member_panel_immediately_after_render(self):
        from ai_meeting_room.product.server import UI_HTML

        self.assertIn("const v101RenderMembersWithoutLocale=renderMembers", UI_HTML)
        self.assertIn("renderMembers=function(s){v101RenderMembersWithoutLocale(s);localizeStatic()}", UI_HTML)

    def test_v101_localizes_formal_audit_event_types_and_sources_before_generic_labels(self):
        from ai_meeting_room.product.server import UI_HTML

        self.assertIn("const v101EventLabels={", UI_HTML)
        self.assertIn("'TaskStarted':'任务已开始'", UI_HTML)
        self.assertIn("'BrainHandoffDecisionApplied':'主脑决策已应用'", UI_HTML)
        self.assertIn("'ManualGPTBrainTransport':'手动 GPT 交接'", UI_HTML)
        self.assertIn("Object.entries(v101EventLabels).sort", UI_HTML)

    def test_summary_export_preview_and_copy_use_chinese_presentation(self):
        from ai_meeting_room.product.server import UI_HTML

        self.assertIn("preview.value=localizeSummaryMarkdown(result.markdown||'')", UI_HTML)
        self.assertIn("## Participants':'## 参与者", UI_HTML)
        self.assertIn("## Pause / Resume Events':'## 暂停 / 恢复事件", UI_HTML)
        self.assertIn("- Agent Result summary:", UI_HTML)
        self.assertIn("- 智能体结果摘要：", UI_HTML)
        self.assertIn("MeetingCompleted','会议已完成", UI_HTML)
        self.assertIn("if(line.startsWith(from))return line.replace(from,to)", UI_HTML)
        self.assertIn("close.textContent='关闭预览'", UI_HTML)
        self.assertIn("close.onclick=()=>{panel.remove();v101RetainedSummaryMarkdown=null;v101RetainedSummaryMeetingId=null}", UI_HTML)

    def test_summary_export_localizes_plain_status_codes_and_transport_metadata(self):
        from ai_meeting_room.product.server import UI_HTML

        self.assertIn("new RegExp('(^|[^A-Za-z0-9_])'+code+'(?=$|[^A-Za-z0-9_])','g')", UI_HTML)
        self.assertIn(".split('transport:').join('传输方式：')", UI_HTML)
        self.assertIn(".split('health:').join('健康状态：')", UI_HTML)
        self.assertIn(".split('[codex]').join('[Codex]')", UI_HTML)

    def test_summary_export_localizes_acceptance_boolean_value(self):
        from ai_meeting_room.product.server import UI_HTML

        self.assertIn("const v101SummaryBooleanBase=localizeSummaryMarkdown", UI_HTML)
        self.assertIn("replace(/(^- 已接受：\\s*)(yes|no|True|False)\\s*$/gm", UI_HTML)
        self.assertIn("({yes:'是',no:'否',True:'是',False:'否'})", UI_HTML)

    def test_summary_export_localizes_machine_completion_and_audit_labels(self):
        from ai_meeting_room.product.server import UI_HTML

        self.assertIn("const v101SummaryDomainLabels={", UI_HTML)
        for source, target in (
            ("NOT_COMPLETED", "尚未完成"),
            ("unassigned", "未分配"),
            ("TaskCreated", "任务已创建"),
            ("BrainHandoffDecisionApplied", "主脑决策已应用"),
        ):
            self.assertIn(source, UI_HTML)
            self.assertIn(target, UI_HTML)

    def test_summary_export_localizes_recovery_reason_and_duplicate_provider_label(self):
        from ai_meeting_room.product.server import UI_HTML

        self.assertIn(".split('explicit 操作员 recovery').join('操作员明确执行的恢复')", UI_HTML)
        self.assertIn(".split('Codex [Codex]').join('Codex')", UI_HTML)
        self.assertIn("String(a.display_name||'').toLowerCase()===String(a.provider||'').toLowerCase()", UI_HTML)

    def test_summary_export_preview_survives_background_refresh(self):
        from ai_meeting_room.product.server import UI_HTML

        self.assertIn("let v101RetainedSummaryMarkdown=null", UI_HTML)
        self.assertIn("showSummaryExport=function(result,preserve=false)", UI_HTML)
        self.assertIn("const v101Phase3cRenderAllWithoutSummaryRetention=phase3cRenderAll", UI_HTML)
        self.assertIn("phase3cRenderAll=function(s){v101Phase3cRenderAllWithoutSummaryRetention(s)", UI_HTML)
        self.assertIn(
            "if(v101RetainedSummaryMeetingId===selected){if(!document.getElementById('meeting-summary-export'))v101ShowSummaryExportBase({markdown:v101RetainedSummaryMarkdown},true)}",
            UI_HTML,
        )
        self.assertIn("if(!document.getElementById('meeting-summary-export'))v101ShowSummaryExportBase", UI_HTML)
        self.assertIn("close.onclick=()=>{panel.remove();v101RetainedSummaryMarkdown=null;v101RetainedSummaryMeetingId=null}", UI_HTML)
        self.assertIn("const v101RenderPreservingSummaryScroll=render", UI_HTML)
        self.assertIn("if(current)current.scrollTop=scrollTop", UI_HTML)
        self.assertIn("if(!preserve){panel.scrollIntoView({block:'center'});preview.focus()}", UI_HTML)


if __name__ == "__main__":
    unittest.main()
