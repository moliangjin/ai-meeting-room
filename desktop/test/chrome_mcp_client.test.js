const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const {
  AUTOCONNECT_ARGS,
  MCP_BROWSER_URL,
  MCP_ARGS,
  MCP_STRUCTURED_CONTENT_FLAG,
  MCP_STATES,
  ChromeDevToolsMcpClient,
  isChatGPTUrl,
  normalizeMcpPage,
  parseListPagesResult,
  readBooleanToolResult,
  safePageMetadata,
} = require('../chrome_mcp_client');

const source = fs.readFileSync(path.join(__dirname, '..', 'chrome_mcp_client.js'), 'utf8');
const mainSource = fs.readFileSync(path.join(__dirname, '..', 'main.js'), 'utf8');

test('test_formal_mcp_uses_browser_url_not_autoconnect', () => {
  assert.deepEqual(MCP_ARGS, ['-y', 'chrome-devtools-mcp@latest', '--browserUrl', 'http://127.0.0.1:9222', '--experimentalStructuredContent']);
  assert.deepEqual(AUTOCONNECT_ARGS, ['-y', 'chrome-devtools-mcp@latest', '--autoConnect']);
  assert.equal(MCP_BROWSER_URL, 'http://127.0.0.1:9222');
  assert.match(source, /new StdioClientTransport/);
  assert.match(source, /command: "npx"/);
  assert.doesNotMatch(source, /MCP_ARGS = \[[^\]]*--autoConnect/);
});

test('test_mcp_process_enables_experimental_structured_content', () => {
  assert.equal(MCP_STRUCTURED_CONTENT_FLAG, '--experimentalStructuredContent');
  assert.equal(MCP_ARGS.includes('--experimentalStructuredContent'), true);
  assert.match(source, /MCP_STRUCTURED_CONTENT_FLAG/);
});

test('test_browser_url_is_loopback_9222', () => {
  assert.match(source, /MCP_BROWSER_URL = "http:\/\/127\.0\.0\.1:9222"/);
  assert.match(source, /--browserUrl/);
  assert.doesNotMatch(source, /--browser-url/);
});

test('MCP initialize and tools list are required before browser connection', () => {
  assert.match(source, /client\.connect\(this\.transport\)/);
  assert.match(source, /client\.listTools\(\)/);
  assert.ok(source.indexOf('client.connect(this.transport)') < source.indexOf('client.listTools()'));
});

test('test_list_pages_is_first_browser_tool', () => {
  assert.match(source, /FIRST_BROWSER_TOOL_MUST_BE_LIST_PAGES/);
  assert.match(source, /connectAndDiscover[\s\S]*_callBrowserTool\("list_pages"/);
  const listPages = source.indexOf('result = await this._callBrowserTool("list_pages"');
  const selectPage = source.indexOf('await this._callBrowserTool("select_page"');
  assert.ok(listPages >= 0 && listPages < selectPage);
});

test('test_waiting_user_authorization_removed_from_browser_url_flow', () => {
  assert.doesNotMatch(source, /WAITING_USER_AUTHORIZATION/);
  assert.match(source, /CONNECTING_BROWSER/);
  assert.match(source, /LISTING_PAGES/);
  assert.match(source, /BINDING_CHATGPT/);
  assert.match(source, /CHECKING_COMPOSER/);
});

test('composer result is parsed as an actual boolean', () => {
  assert.equal(readBooleanToolResult({structuredContent: {result: true}, content: []}), true);
  assert.equal(readBooleanToolResult({structuredContent: {result: false}, content: [{text: 'true'}]}), false);
  assert.equal(readBooleanToolResult({content: [{text: '{"result":true}'}]}), true);
  assert.equal(readBooleanToolResult({content: []}), false);
});

test('test_chatgpt_page_selected_only', () => {
  assert.equal(isChatGPTUrl('https://chatgpt.com/'), true);
  assert.equal(isChatGPTUrl('https://chatgpt.com/g/g-p-123/c/abc'), true);
  assert.equal(isChatGPTUrl('https://accounts.google.com/'), false);
  assert.equal(isChatGPTUrl('https://gmail.com/'), false);
});

test('test_non_chatgpt_page_not_read', () => {
  const pages = safePageMetadata({pages: [
    {id: 1, url: 'https://gmail.com/'},
    {id: 2, url: 'https://chatgpt.com/c/abc?secret=removed'},
  ]});
  assert.equal(pages.length, 2);
  assert.equal(pages[1].url, 'https://chatgpt.com/c/abc');
  assert.match(source, /Other page metadata is discarded/);
  assert.match(source, /select_page/);
});

test('test_call_tool_result_parsed_via_mcp_sdk_shape', () => {
  const result = parseListPagesResult({structuredContent: {pages: [
    {id: 1, url: 'https://gmail.com/'},
    {id: 2, url: 'https://chatgpt.com/c/abc?redacted=1'},
  ]}, content: []});
  assert.equal(result.resultType, 'structuredContent');
  assert.equal(result.pages.length, 2);
  assert.equal(result.pages[1].url, 'https://chatgpt.com/c/abc');
});

test('test_call_tool_result_content_text_payload_supported', () => {
  const result = parseListPagesResult({content: [{type: 'text', text: [
    '## Pages',
    '7: ChatGPT (https://chatgpt.com/) [selected]',
  ].join('\n')}]});
  assert.equal(result.resultType, 'content.text');
  assert.equal(result.pages[0].pageId, 7);
  assert.equal(result.pages[0].selected, true);
});

test('test_structured_pages_is_primary_path', () => {
  const result = parseListPagesResult({
    structuredContent: {pages: [{id: 4, url: 'https://chatgpt.com/c/primary', title: 'Primary'}]},
    content: [{type: 'text', text: 'this is intentionally not a page list'}],
  });
  assert.equal(result.resultType, 'structuredContent');
  assert.equal(result.pages[0].pageId, 4);
});

test('test_current_official_list_pages_text_format', () => {
  const fixture = fs.readFileSync(path.join(__dirname, 'fixtures', 'list_pages_official.txt'), 'utf8');
  const result = parseListPagesResult({content: [{type: 'text', text: fixture}]});
  assert.deepEqual(result.pages.map(page => ({pageId: page.pageId, url: page.url, selected: page.selected})), [
    {pageId: 0, url: 'https://chatgpt.com/', selected: true},
    {pageId: 1, url: 'chrome://inspect/', selected: false},
    {pageId: 2, url: 'https://chatgpt.com/c/example', selected: false},
  ]);
});

test('test_text_format_with_page_title', () => {
  const result = parseListPagesResult({content: [{type: 'text', text: '1: Work Chat (https://chatgpt.com/c/work)'}]});
  assert.equal(result.pages[0].title, 'Work Chat');
});

test('test_text_title_with_parentheses', () => {
  const result = parseListPagesResult({content: [{type: 'text', text: '1: Project (Test) - ChatGPT (https://chatgpt.com/c/test)'}]});
  assert.equal(result.pages[0].title, 'Project (Test) - ChatGPT');
});

test('test_chrome_scheme_page_parses_but_is_filtered', () => {
  const result = parseListPagesResult({content: [{type: 'text', text: '1: Inspect with Chrome Developer Tools (chrome://inspect/#remote-debugging)'}]});
  assert.equal(result.pages[0].pageId, 1);
  assert.equal(isChatGPTUrl(result.pages[0].url), false);
});

test('test_chatgpt_page_selected', () => {
  const result = parseListPagesResult({content: [{type: 'text', text: [
    '0: Other (https://example.com/)',
    '2: ChatGPT (https://chatgpt.com/c/abc) [selected]',
  ].join('\n')}]});
  const target = result.pages.find(page => isChatGPTUrl(page.url));
  assert.equal(target.pageId, 2);
  assert.equal(target.selected, true);
});

test('test_structured_content_bypasses_text_parser', () => {
  const result = parseListPagesResult({
    structuredContent: {pages: [{pageId: '8', url: 'https://chatgpt.com/'}]},
    content: [{type: 'text', text: 'garbage that must not be parsed'}],
  });
  assert.equal(result.resultType, 'structuredContent');
  assert.equal(result.pages[0].pageId, 8);
});

test('test_safe_shape_diagnostics_only', () => {
  const result = parseListPagesResult({structuredContent: {pages: [
    {id: 1, title: 'Private title', url: 'https://chatgpt.com/c/abc?token=secret'},
  ]}, content: [{type: 'text', text: 'ignored'}]});
  assert.deepEqual(result.diagnostics.firstPageKeys, ['id', 'title', 'url']);
  assert.equal('url' in result.diagnostics, false);
  assert.equal('title' in result.diagnostics, false);
  assert.equal(JSON.stringify(result.diagnostics).includes('secret'), false);
});

test('test_no_cookie_or_token_logged', () => {
  const result = parseListPagesResult({structuredContent: {pages: [{id: 1, url: 'https://chatgpt.com/'}], token: 'secret', cookie: 'secret'}, content: []});
  const diagnostics = JSON.stringify(result.diagnostics).toLowerCase();
  assert.equal(diagnostics.includes('cookie'), false);
  assert.equal(diagnostics.includes('token'), false);
  assert.equal(diagnostics.includes('authorization'), false);
});

test('test_list_pages_parse_failure_has_explicit_error', () => {
  assert.throws(() => parseListPagesResult({content: [{type: 'text', text: 'not page data'}]}), error => {
    assert.equal(error.code, 'MCP_TEXT_PAGES_PARSE_FAILED');
    assert.equal(error.contentType, 'text');
    assert.equal(error.contentItemCount, 1);
    return true;
  });
});

test('test_structured_page_normalization', () => {
  assert.deepEqual(normalizeMcpPage({pageId: '9', url: 'https://chatgpt.com/c/x?redacted=1', title: 'ChatGPT (work)', selected: true}), {
    pageId: 9, url: 'https://chatgpt.com/c/x', title: 'ChatGPT (work)', selected: true,
  });
});

test('test_text_fallback_handles_title_with_parentheses', () => {
  const result = parseListPagesResult({content: [{type: 'text', text: [
    '## Pages',
    '1: Team room (Q3) (https://chatgpt.com/c/room) [selected]',
  ].join('\n')}]});
  assert.equal(result.pages[0].title, 'Team room (Q3)');
  assert.equal(result.pages[0].url, 'https://chatgpt.com/c/room');
});

test('test_chrome_inspect_page_is_ignored', () => {
  const result = parseListPagesResult({structuredContent: {pages: [
    {id: 1, title: 'Remote debugging', url: 'chrome://inspect/#remote-debugging'},
    {id: 2, title: 'ChatGPT', url: 'https://chatgpt.com/'},
  ]}, content: []});
  const chatgpt = result.pages.filter(page => isChatGPTUrl(page.url));
  assert.equal(chatgpt.length, 1);
  assert.equal(chatgpt[0].pageId, 2);
});

test('test_missing_structured_pages_uses_text_fallback', () => {
  const result = parseListPagesResult({structuredContent: {other: true}, content: [
    {type: 'text', text: '1: ChatGPT (https://chatgpt.com/) [selected]'},
  ]});
  assert.equal(result.resultType, 'content.text');
  assert.equal(result.pages.length, 1);
});

test('test_invalid_pages_has_explicit_error', () => {
  assert.throws(() => parseListPagesResult({structuredContent: {pages: {bad: true}}, content: []}), error => {
    assert.equal(error.code, 'MCP_STRUCTURED_PAGES_INVALID');
    assert.equal(error.diagnostics.structuredPagesIsArray, false);
    return true;
  });
});

test('test_structured_pages_missing_is_distinct_without_text_fallback', () => {
  assert.throws(() => parseListPagesResult({structuredContent: {status: 'ok'}, content: []}), error => {
    assert.equal(error.code, 'MCP_STRUCTURED_PAGES_MISSING');
    return true;
  });
});

test('test_parser_diagnostics_do_not_log_page_content', () => {
  const result = parseListPagesResult({structuredContent: {pages: [
    {id: 1, title: 'Private title', url: 'https://chatgpt.com/'},
  ]}, content: []});
  assert.equal(result.diagnostics.structuredPagesCount, 1);
  assert.equal('title' in result.diagnostics, false);
  assert.equal(JSON.stringify(result.diagnostics).includes('Private'), false);
});

test('test_parser_diagnostics_do_not_log_cookie_or_token', () => {
  const result = parseListPagesResult({structuredContent: {pages: [
    {id: 1, url: 'https://chatgpt.com/?token=redacted'},
  ]}, content: []});
  const diagnostics = JSON.stringify(result.diagnostics);
  assert.equal(diagnostics.includes('token'), false);
  assert.equal(diagnostics.includes('cookie'), false);
});

test('test_cookie_never_read', () => {
  assert.doesNotMatch(source, /get_cookies|storage_state|list_network_requests|Authorization/);
  assert.match(source, /cookieReadByApp: "NONE"/);
});

test('test_token_never_read', () => {
  assert.doesNotMatch(source, /get_cookies|storage_state|list_network_requests|Authorization/);
  assert.match(source, /tokenReadByApp: "NONE"/);
});

test('MCP process loss enters fail-closed callback', () => {
  assert.match(source, /transport\.onclose/);
  assert.match(source, /MCP_PROCESS_LOST/);
  assert.match(source, /onFailure\(state, code\)/);
});

test('historical MCP client remains isolated from formal runtime', () => {
  assert.match(source, /if \(this\.client && !this\._closed\) return/);
  assert.doesNotMatch(mainSource, /chromeMcpClient|ChromeDevToolsMcpClient|MCP_STATES/);
  assert.match(mainSource, /FORMAL_BRAIN_OPEN_PATH/);
});

test('test_remote_debugging_unavailable_has_explicit_error', () => {
  const client = new ChromeDevToolsMcpClient();
  assert.equal(client.browserConnectionTimeoutMs, 60000);
  assert.equal(MCP_STATES.CONNECTING_BROWSER, 'CONNECTING_BROWSER');
  assert.match(source, /MCP_LIST_PAGES_TIMEOUT/);
  assert.match(source, /REMOTE_DEBUGGING_NOT_AVAILABLE/);
  assert.match(source, /MCP_EXECUTABLE_NOT_FOUND/);
  assert.match(source, /MCP_PROCESS_START_FAILED/);
  assert.match(source, /MCP_INITIALIZE_FAILED/);
  assert.match(source, /MCP_TOOLS_LIST_FAILED/);
  assert.match(source, /MCP_CONNECTION_REJECTED/);
  assert.match(source, /CHATGPT_TARGET_NOT_FOUND/);
  assert.match(source, /COMPOSER_NOT_FOUND/);
});

test('test_mcp_error_never_maps_to_unknown_without_detail', () => {
  assert.match(source, /LAST_MCP_ERROR_CODE/);
  assert.match(source, /LAST_MCP_ERROR_STAGE/);
  assert.match(source, /REMOTE_DEBUGGING_NOT_AVAILABLE/);
  assert.match(source, /MCP_LIST_PAGES_RESULT_PARSE_FAILED/);
  assert.match(source, /sanitizeErrorMessage/);
});

test('test_composer_check_after_select_page', () => {
  assert.ok(source.indexOf('await this._callBrowserTool("select_page"') < source.indexOf('this.state = MCP_STATES.CHECKING_COMPOSER'));
  assert.ok(source.indexOf('this.state = MCP_STATES.CHECKING_COMPOSER') < source.indexOf('const composer = await this._callBrowserTool("evaluate_script"'));
});

test('Product Shell and Electron use formal Playwright attach route', () => {
  assert.match(mainSource, /FORMAL_BRAIN_OPEN_PATH/);
  assert.match(mainSource, /postJson\(`http:\/\/127\.0\.0\.1:\$\{PRODUCT_PORT\}\$\{FORMAL_BRAIN_OPEN_PATH\}/);
  assert.doesNotMatch(mainSource, /connectAndDiscover\(\)/);
  assert.match(mainSource, /PLAYWRIGHT_ATTACH_EXISTING_REAL_CHROME/);
});
