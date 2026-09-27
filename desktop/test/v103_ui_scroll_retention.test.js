const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const vm = require('node:vm');

const source = fs.readFileSync(path.join(__dirname, '..', '..', 'ai_meeting_room', 'product', 'server.py'), 'utf8');

function interactionStateFunctions() {
  const names = ['readUiInteractionState', 'restoreUiInteractionState'];
  const lines = names.map(name => {
    const line = source.split('\n').find(item => item.startsWith(`function ${name}(`));
    assert.ok(line, `${name} must exist in the Product Shell`);
    return line;
  });
  return `${lines.join('\n')}\n({readUiInteractionState,restoreUiInteractionState})`;
}

test('polling preserves viewport and task form position while restoring focus', () => {
  const window = {
    scrollX: 0,
    scrollY: 1200,
    scrollTo(x, y) { this.scrollX = x; this.scrollY = y; },
  };
  const main = {scrollTop: 350, scrollLeft: 0};
  const right = {scrollTop: 80, scrollLeft: 0};
  const oldInput = {id: 'taskTitle', value: '只读任务', checked: false};
  let focusedWith;
  const replacement = {
    id: 'taskTitle', tagName: 'INPUT', type: 'text', value: '',
    focus(options) {
      focusedWith = options;
      if (!options?.preventScroll) window.scrollY = 0;
    },
  };
  let inputs = [oldInput];
  const document = {
    activeElement: oldInput,
    querySelector(selector) {
      if (selector === '.main') return main;
      if (selector === '.right') return right;
      return null;
    },
    querySelectorAll(selector) {
      if (selector === '#detail input, #detail textarea, #detail select') return inputs;
      if (selector === '#detail details') return [];
      return [];
    },
    getElementById(id) { return id === 'taskTitle' ? replacement : null; },
  };
  const context = {
    window, document,
    desktopBrainPocInFlight: false,
    desktopBrainPocUiState: {phase: 'NOT_RUN', detail: ''},
    desktopConnectInFlight: false,
    $: id => document.getElementById(id),
    setDesktopConnectUiState() {},
    setDesktopPocStatus() {},
  };
  const {readUiInteractionState, restoreUiInteractionState} = vm.runInNewContext(interactionStateFunctions(), context);
  const saved = readUiInteractionState();
  inputs = [replacement];
  document.activeElement = null;
  window.scrollY = 0;
  main.scrollTop = 0;
  right.scrollTop = 0;
  restoreUiInteractionState(saved);

  assert.equal(replacement.value, '只读任务');
  assert.equal(window.scrollY, 1200);
  assert.equal(main.scrollTop, 350);
  assert.equal(right.scrollTop, 80);
  assert.equal(focusedWith?.preventScroll, true);
});

test('the outermost render restores scroll after Phase3C inserts its dashboard', () => {
  assert.match(source, /const v103FinalRender=render;render=function\(s\)\{let state=readUiInteractionState\(\);v103FinalRender\(s\);restoreUiInteractionState\(state\)\}/);
});

test('manual GPT controls cannot overflow their responsive card', () => {
  assert.match(source, /#brainHandoffTask[^']*max-width:100%/);
  assert.match(source, /\.handoff-steps>\*\{min-width:0\}/);
});

test('completed Meeting and handoff empty state use accurate Chinese labels', () => {
  assert.match(source, /s\.meeting\.status==='COMPLETED'\?'会议已完成':'等待任务完成'/);
  assert.match(source, /点击“生成 \/ 查看”后显示通过安全检查的数据包。/);
  const locale = JSON.parse(fs.readFileSync(path.join(__dirname, '..', '..', 'ai_meeting_room', 'locales', 'zh-CN.json'), 'utf8'));
  assert.equal(locale.status.CONSUMED, '已应用');
});

test('localization leaves machine paths untouched while translating product labels', () => {
  const line = source.split('\n').find(item => item.startsWith('const v103BaseLocalizeText=localizeText;'));
  assert.ok(line);
  const context = {localizeText(value) { return String(value).replaceAll('ready', '已就绪'); }};
  vm.runInNewContext(line, context);
  assert.equal(context.localizeText('/path/to/ai-meeting-room/workspace'), '/path/to/ai-meeting-room/workspace');
  assert.equal(context.localizeText('ready'), '已就绪');
  assert.equal(context.localizeText('Rework'), '返工');
  assert.equal(context.localizeText('process-restart'), '程序重启');
  assert.equal(context.localizeText('process restarted while meeting was RUNNING; health confirmation required'), '程序重启时会议仍在运行；须通过健康检查后手动恢复');
  assert.equal(context.localizeText('process restarted during recovery; explicit health confirmation required'), '恢复过程中程序重启；须重新通过健康检查并手动恢复');
});
