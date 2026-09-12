const $ = (id) => document.getElementById(id);
const clone = (value) => structuredClone(value);
const bridge = window.AstrBotPluginPage;
let state = { items: [], revision: null };
let draft = {}, originalName = null, editRevision = null, mode = 'form', dirty = false, busy = false;
let confirmResolve = null;
let platforms = [], platformLoading = false;
const blank = () => ({ name: '', description: '', enabled: false, parameters: { type: 'object', properties: {} }, request: { method: 'GET', url: '' } });
const own = (object, key) => Object.hasOwn(object, key);
const object = (value) => value !== null && typeof value === 'object' && !Array.isArray(value);

const noticeTimers = new WeakMap();
function notice(message, error = false, editor = false) {
  const box = $(editor ? 'editor-error' : 'notice');
  clearTimeout(noticeTimers.get(box));
  box.textContent = message;
  box.classList.toggle('error', error);
  box.hidden = !message;
  if (message && !error && !editor) {
    noticeTimers.set(box, setTimeout(() => { box.hidden = true; box.textContent = ''; }, 4000));
  }
}
function el(tag, text, className) {
  const node = document.createElement(tag);
  if (text !== undefined) node.textContent = text;
  if (className) node.className = className;
  return node;
}
function confirmAction(title, text, button = '确认') {
  $('confirm-title').textContent = title;
  $('confirm-text').textContent = text;
  $('confirm-yes').textContent = button;
  $('confirm-dialog').showModal();
  return new Promise((resolve) => { confirmResolve = resolve; });
}
function finishConfirm(value) {
  $('confirm-dialog').close();
  confirmResolve?.(value);
  confirmResolve = null;
}
$('confirm-yes').onclick = () => finishConfirm(true);
$('confirm-no').onclick = () => finishConfirm(false);
$('confirm-dialog').addEventListener('cancel', (event) => { event.preventDefault(); finishConfirm(false); });

async function refresh() {
  state = await bridge.apiGet('catalog');
  renderList();
  if (state.error) notice(`原有配置需要修复：${state.error}\n请在插件配置中修复 JSON 后刷新；原始数据未被覆盖。`, true);
}
let activeAccount = null;
function actionButton(label, callback, style = '') {
  const button = el('button', label, style); button.disabled = busy;
  button.onclick = () => Promise.resolve(callback()).catch((error) => notice(error.message, true));
  return button;
}
function operationCard(item) {
  const card = el('article', undefined, 'card operation-card');
  const top = el('div', undefined, 'card-top');
  top.append(el('span', item.display_name || `api_${item.name}`, 'card-title'), el('span', item.request.method, 'method'), el('span', item.enabled === false ? '已停用' : '已启用', `badge${item.enabled === false ? ' off' : ''}`));
  const actions = el('div', undefined, 'card-actions');
  actions.append(actionButton('编辑', () => openEditor(item)), actionButton(item.enabled === false ? '启用' : '停用', () => toggle(item)), actionButton('删除', () => remove(item), 'danger'));
  const heading = el('div', undefined, 'operation-heading'); heading.append(top, actions);
  const details = el('details', undefined, 'operation-details');
  details.append(el('summary', '接口详情'), el('p', item.request.url, 'endpoint'), el('p', item.description, 'muted'));
  const name = el('p', state.tool_names?.[item.name] || item.tool_name || `api_${item.name}`, 'operation-call-name');
  card.append(heading, name, details); return card;
}
function clearOperationFilters() {
  $('operation-search').value = ''; $('operation-status').value = ''; $('operation-method').value = '';
}
function renderAccountOperations(items) {
  const term = $('operation-search').value.trim().toLowerCase();
  const status = $('operation-status').value, method = $('operation-method').value;
  const matches = items.filter((item) => (!method || item.request.method === method)
    && (!status || (item.enabled !== false) === (status === 'enabled'))
    && [item.name, item.display_name || '', state.tool_names?.[item.name] || item.tool_name || '', item.request.url, item.description].some((value) => value.toLowerCase().includes(term)));
  $('operation-count').textContent = `显示 ${matches.length} / ${items.length} 个操作`;
  $('account-operation-list').replaceChildren(...matches.map(operationCard));
  if (!matches.length) {
    const empty = el('div', undefined, 'operation-empty');
    empty.append(el('h3', '没有匹配的操作'), el('p', '尝试其他关键词，或清除筛选查看全部操作。', 'muted'), actionButton('查看全部操作', () => { clearOperationFilters(); renderList(); }));
    $('account-operation-list').append(empty);
  }
}
function enterAccount(connection) { activeAccount = connection.id; clearOperationFilters(); renderList(); }
$('operation-search').oninput = () => renderAccountOperations(state.items.filter((item) => item.connection?.id === activeAccount));
$('operation-status').onchange = $('operation-method').onchange = () => renderList();
$('clear-operation-filters').onclick = () => { clearOperationFilters(); renderList(); };
$('back-accounts').onclick = () => { activeAccount = null; renderList(); };
function renderList() {
  const accountItems = state.items.filter((item) => item.connection?.id === activeAccount);
  if (activeAccount && !accountItems.length) activeAccount = null;
  $('page-header').hidden = !!activeAccount;
  $('overview').hidden = !!activeAccount; $('account-page').hidden = !activeAccount;
  if (activeAccount) {
    const connection = accountItems[0].connection;
    $('account-page-title').textContent = connection.name;
    $('account-page-count').textContent = `${accountItems.length} 个操作 · ${accountItems.filter((item) => item.enabled !== false).length} 个启用`;
    renderAccountOperations(accountItems);
    $('account-settings').disabled = $('account-delete').disabled = busy;
    $('account-settings').onclick = () => openConnection(connection);
    $('account-delete').onclick = () => deleteConnection(connection).catch((error) => notice(error.message, true));
  }
  const list = $('list'); list.replaceChildren();
  const term = $('search').value.trim().toLowerCase();
  const items = state.items.filter((item) => (!$('method-filter').value || item.request.method === $('method-filter').value) && [item.name, item.display_name || '', item.connection?.name || '', item.connection?.platform || '', item.description, item.request.url].some((v) => v.toLowerCase().includes(term)));
  const total = new Set(state.items.map((item) => item.connection ? 'connection:' + item.connection.id : 'tool:' + item.name)).size;
  $('count').textContent = `${total} 个接入 · ${state.items.length} 个操作 · ${state.items.filter((item) => item.enabled !== false).length} 个启用`;
  if (!items.length) {
    const filtered = !!term || !!$('method-filter').value;
    const empty = el('div', undefined, 'empty');
    empty.append(el('div', '{ }', 'symbol'), el('h2', filtered ? '没有匹配的 API' : '接入你的第一个 API'), el('p', filtered ? '试试其他名称、地址或请求方法。' : '选择平台并填写 Token，或手动添加接口。', 'muted'));
    if (!filtered) empty.append(actionButton('＋ 新增 API', () => openEditor(), 'primary'));
    list.append(empty); return;
  }
  const groups = new Map();
  for (const item of items) {
    const id = item.connection ? 'connection:' + item.connection.id : 'tool:' + item.name;
    if (!groups.has(id)) groups.set(id, []);
    groups.get(id).push(item);
  }
  for (const members of groups.values()) {
    const connection = members[0].connection;
    if (!connection) { list.append(operationCard(members[0])); continue; }
    const all = state.items.filter((item) => item.connection?.id === connection.id);
    const card = el('section', undefined, 'connection-card'); card.dataset.connectionId = connection.id;
    const header = el('div', undefined, 'connection-header');
    const heading = el('div'); heading.append(el('h2', connection.name), el('p', `${all.length} 个操作 · ${all.filter((item) => item.enabled !== false).length} 个启用`, 'muted'));
    const actions = el('div', undefined, 'card-actions'); actions.append(actionButton('进入账户', () => enterAccount(connection)), actionButton('删除接入', () => deleteConnection(connection), 'danger'));
    const brand = el('div', undefined, 'integration-head'); brand.append(el('strong', 'AppLovin Report'), el('span', all.some((item) => item.enabled !== false) ? '● 已启用' : '未启用', 'badge'));
    heading.className = 'integration-body';
    header.append(brand, heading, actions);
    card.append(header); list.append(card);
  }
}
let connectionDraft = null, connectionRevision = null, connectionDirty = false;
function openConnection(connection) {
  connectionDraft = connection; connectionRevision = state.revision; connectionDirty = false;
  $('connection-call-name').value = connection.call_name || '';
  $('connection-name').value = connection.name; $('connection-token').value = ''; $('connection-error').hidden = true;
  $('connection-operations').replaceChildren();
  for (const item of state.items.filter((value) => value.connection?.id === connection.id)) {
    const control = checkbox(item.enabled !== false); control.dataset.operationName = item.name;
    const label = el('label', item.display_name || item.name, 'check'); label.prepend(control);
    const row = el('div', undefined, 'connection-permission'); row.append(label, el('span', item.request.method, 'method')); $('connection-operations').append(row);
  }
  $('connection-editor').showModal();
}
async function closeConnection() {
  if (busy) return;
  if (connectionDirty && !await confirmAction('放弃修改？', '接入名称、Token 和权限修改尚未保存。', '放弃修改')) return;
  $('connection-editor').close(); $('connection-token').value = ''; connectionDraft = null;
}
$('close-connection').onclick = $('cancel-connection').onclick = closeConnection;
$('connection-editor').addEventListener('cancel', (event) => { event.preventDefault(); closeConnection(); });
$('connection-editor').addEventListener('input', () => { connectionDirty = true; });
$('save-connection').onclick = async () => {
  if (busy || !connectionDraft) return;
  const controls = [...$('connection-editor').querySelectorAll('button,input')]; controls.forEach((node) => node.disabled = true);
  try {
    const callName = $('connection-call-name').value.trim();
    if (callName && !/^[A-Za-z][A-Za-z_]{0,39}$/.test(callName)) throw new Error('调用名称仅允许英文字母和下划线，英文开头，最多 40 位');
    await mutate('update-connection', { revision: connectionRevision, connection_id: connectionDraft.id, ...(callName ? { call_name: callName } : {}), name: $('connection-name').value, token: $('connection-token').value, enabled_names: [...$('connection-operations').querySelectorAll('input:checked')].map((node) => node.dataset.operationName) });
    connectionDirty = false; $('connection-editor').close(); $('connection-token').value = ''; notice('接入名称、Token 和操作权限已保存。');
  } catch (error) { $('connection-error').textContent = error.message; $('connection-error').hidden = false; }
  finally { controls.forEach((node) => node.disabled = false); }
};
async function deleteConnection(connection) {
  const revision = state.revision;
  if (!await confirmAction('删除接入', `确定删除“${connection.name}”及其全部操作？其他账户不受影响。`, '删除接入')) return;
  await mutate('delete-connection', { revision, connection_id: connection.id }); notice('接入已删除。');
}
async function mutate(endpoint, payload) {
  busy = true; renderList();
  try { state = await bridge.apiPost(endpoint, payload); renderList(); }
  finally { busy = false; renderList(); }
}
async function toggle(item) {
  await mutate('save', { revision: state.revision, original_name: item.name, definition: { ...clone(item), enabled: item.enabled === false } });
  notice(`${item.display_name || item.name} 已${item.enabled === false ? '启用' : '停用'}，立即生效。`);
}
async function remove(item) {
  const revision = state.revision;
  if (!await confirmAction('删除 API', `确定删除 ${item.display_name || item.name}？\n将移除接口定义及其工具，不会调用目标 API。`, '删除 API')) return;
  await mutate('delete', { revision, name: item.name }); notice(`${item.display_name || item.name} 已删除。`);
}

function selectOptions(values, selected) {
  const select = el('select');
  for (const value of values) { const option = el('option', Array.isArray(value) ? value[1] : value); option.value = Array.isArray(value) ? value[0] : value; select.append(option); }
  select.value = selected; return select;
}
function labeled(text, input) { const label = el('label', text); label.append(input); return label; }
function input(value = '', placeholder = '') { const node = el('input'); node.value = value; node.placeholder = placeholder; return node; }
function checkbox(value) { const node = input(); node.type = 'checkbox'; node.checked = value; return node; }
function removeRow(row) { const button = el('button', '✕', 'remove'); button.setAttribute('aria-label', '删除字段'); button.onclick = () => { row.remove(); dirty = true; updateParamNames(); }; return button; }
function valueType(value) { return value === null ? 'null' : Array.isArray(value) ? 'array' : typeof value === 'object' ? 'object' : typeof value; }
function valueText(value) { return typeof value === 'string' ? value : JSON.stringify(value); }
function typedValue(text, type) {
  if (type === 'string') return text;
  if (type === 'null') return null;
  let value;
  try { value = JSON.parse(text); } catch { throw new Error(`${type} 类型的值格式不正确`); }
  if (type === 'integer' ? !Number.isInteger(value) : valueType(value) !== type) throw new Error(`请填写有效的 ${type} 类型值`);
  return value;
}
function updateParamNames() {
  $('parameter-names').replaceChildren();
  for (const row of $('params').children) { const name = row.fields.name.value.trim(); if (name) { const option = el('option'); option.value = name; $('parameter-names').append(option); } }
}
function addParam(name = '', schema = { type: 'string' }, required = false) {
  const row = el('div', undefined, 'param-row');
  const controls = { name: input(name, '如 item_id'), type: selectOptions(['string', 'integer', 'number', 'boolean', 'object', 'array'], schema.type || 'string'), description: input(schema.description || '', '帮助模型理解参数'), required: checkbox(required), hasDefault: checkbox(own(schema, 'default')), value: input(own(schema, 'default') ? valueText(schema.default) : '', '默认值') };
  const main = el('div', undefined, 'row-main'); main.append(labeled('参数名', controls.name), labeled('类型', controls.type), labeled('说明', controls.description), removeRow(row));
  const options = el('div', undefined, 'row-options'); options.append(labeled('必填', controls.required), labeled('默认值', controls.hasDefault), controls.value);
  controls.value.disabled = !controls.hasDefault.checked;
  controls.hasDefault.onchange = () => { controls.value.disabled = !controls.hasDefault.checked; };
  controls.name.oninput = updateParamNames;
  row.fields = controls; row.schema = clone(schema); row.append(main, options); $('params').append(row); updateParamNames();
}
function addMapping(container, key = '', value = '') {
  const row = el('div', undefined, 'map-row');
  const isParam = object(value) && Object.keys(value).length === 1 && own(value, '$param');
  const controls = { key: input(key, '字段名'), type: selectOptions([['param', '模型参数'], ['string', '固定文本'], ['number', '数字'], ['boolean', '布尔值'], ['null', 'null'], ['object', '对象'], ['array', '数组']], isParam ? 'param' : valueType(value)), value: input(isParam ? value.$param : valueText(value), '值或参数名') };
  const update = () => { controls.value.disabled = controls.type.value === 'null'; if (controls.type.value === 'param') controls.value.setAttribute('list', 'parameter-names'); else controls.value.removeAttribute('list'); };
  controls.type.onchange = update; update();
  const main = el('div', undefined, 'row-main'); main.append(labeled('字段名', controls.key), labeled('值来源', controls.type), labeled('值 / 参数名', controls.value), removeRow(row)); row.fields = controls; row.append(main); $(container).append(row);
}
function readMappings(container) {
  const values = Object.create(null);
  for (const row of $(container).children) {
    const { key, type, value } = row.fields;
    const name = key.value.trim();
    if (!name) throw new Error('请求字段名称不能为空');
    if (own(values, name)) throw new Error('请求字段名称重复');
    values[name] = type.value === 'param' ? { $param: value.value.trim() } : typedValue(value.value, type.value);
  }
  return values;
}
function assertFormSupported(value) {
  if (!object(value) || !object(value.request)) throw new Error('接口定义必须包含 request 对象');
  const parameters = value.parameters || { type: 'object', properties: {} };
  for (const schema of Object.values(parameters.properties || {})) {
    if (!object(schema) || !['string', 'integer', 'number', 'boolean', 'object', 'array'].includes(schema.type)) throw new Error('此接口包含复杂参数类型，请继续使用 JSON 编辑；原始定义会完整保留。');
  }
}
function renderForm() {
  assertFormSupported(draft);
  $('tool-name').readOnly = !!draft.connection?.call_name;
  $('tool-name').value = draft.connection?.call_name ? `${draft.connection.call_name}_${draft.connection.operation}` : draft.tool_name || '';
  $('tool-name').placeholder = state.tool_names?.[draft.name] || '留空按自定义名称生成';
  $('display-name').value = draft.display_name || '';
  $('name').value = draft.name || ''; $('description').value = draft.description || ''; $('enabled').checked = draft.enabled !== false;
  $('method').value = draft.request.method || 'GET'; $('url').value = draft.request.url || '';
  $('params').replaceChildren();
  for (const [name, schema] of Object.entries(draft.parameters?.properties || {})) addParam(name, schema, (draft.parameters.required || []).includes(name));
  for (const key of ['query', 'headers']) { $(`map-${key}`).replaceChildren(); for (const [name, value] of Object.entries(draft.request[key] || {})) addMapping(`map-${key}`, name, value); }
  $('headers-details').open = Object.keys(draft.request.headers || {}).length > 0;
  $('map-body').replaceChildren();
  if (own(draft.request, 'json')) {
    const body = draft.request.json;
    if (object(body) && !own(body, '$param')) { $('body-kind').value = 'json'; for (const [key, value] of Object.entries(body)) addMapping('map-body', key, value); }
    else { $('body-kind').value = 'raw'; $('body-raw').value = JSON.stringify(body, null, 2); }
  } else if (own(draft.request, 'form')) { $('body-kind').value = 'form'; for (const [key, value] of Object.entries(draft.request.form)) addMapping('map-body', key, value); }
  else { $('body-kind').value = 'none'; }
  bodyVisibility();
  const response = draft.response || {};
  $('pointer').value = response.pointer || ''; $('timeout').value = draft.request.timeout ?? 30;
  $('preview').value = response.preview_chars ?? 4000; $('max-bytes').value = response.max_bytes ?? 5242880; $('save-result').checked = response.save || false;
}
function readForm() {
  const value = clone(draft);
  if ($('tool-name').value.trim()) value.tool_name = $('tool-name').value.trim(); else delete value.tool_name;
  if ($('display-name').value.trim()) value.display_name = $('display-name').value.trim(); else delete value.display_name;
  value.name = $('name').value.trim(); value.description = $('description').value.trim(); value.enabled = $('enabled').checked;
  value.parameters ||= { type: 'object' };
  const properties = Object.create(null), required = [];
  for (const row of $('params').children) {
    const fields = row.fields, name = fields.name.value.trim();
    if (!name) throw new Error('模型参数名称不能为空');
    if (own(properties, name)) throw new Error('模型参数名称重复');
    const schema = clone(row.schema);
    schema.type = fields.type.value; schema.description = fields.description.value;
    if (fields.hasDefault.checked) schema.default = typedValue(fields.value.value, schema.type); else delete schema.default;
    properties[name] = schema;
    if (fields.required.checked) required.push(name);
  }
  value.parameters.properties = properties; value.parameters.required = required;
  value.request.method = $('method').value; value.request.url = $('url').value.trim();
  value.request.timeout = Number($('timeout').value);
  for (const key of ['query', 'headers']) { const mapping = readMappings(`map-${key}`); if (Object.keys(mapping).length || own(value.request, key)) value.request[key] = mapping; }
  delete value.request.json; delete value.request.form;
  const kind = $('body-kind').value;
  if (kind === 'json' || kind === 'form') value.request[kind] = readMappings('map-body');
  if (kind === 'raw') { try { value.request.json = JSON.parse($('body-raw').value); } catch { throw new Error('完整请求体不是有效 JSON'); } }
  value.response = { ...(value.response || {}), pointer: $('pointer').value, preview_chars: Number($('preview').value), max_bytes: Number($('max-bytes').value), save: $('save-result').checked };
  return value;
}
function readJSON() { let value; try { value = JSON.parse($('json-text').value); } catch { throw new Error('JSON 格式错误，请检查括号和引号'); } if (!object(value)) throw new Error('请填写单个接口对象，不是数组'); return value; }
function bodyVisibility() { const kind = $('body-kind').value; $('body-fields').hidden = !['json', 'form'].includes(kind); $('body-raw-label').hidden = kind !== 'raw'; }
function displayMode(next) {
  mode = next;
  const market = next === 'platform' && $('platform-setup').hidden;
  $('editor').classList.toggle('market-mode', market);
  $('save').hidden = market;
  for (const key of ['platform', 'form', 'curl', 'json']) { $(`panel-${key}`).hidden = key !== next; $(`tab-${key}`).setAttribute('aria-selected', String(key === next)); }
  $('save').disabled = next === 'curl' || (next === 'platform' && ($('platform-setup').hidden || !platforms.some((item) => item.id === $('platform-select').value)));
  $('save').textContent = next === 'platform' ? '保存接入与权限' : '保存并生效';
}
function switchMode(next) {
  if (mode === next) return;
  try {
    if (mode === 'form') draft = readForm();
    else if (mode === 'json') draft = readJSON();
    if (next === 'form') renderForm();
    if (next === 'json') $('json-text').value = JSON.stringify(draft, null, 2);
    notice('', false, true); displayMode(next);
  } catch (error) { notice(error.message, true, true); }
}
function openEditor(item) {
  originalName = item?.name ?? null; editRevision = state.revision; draft = clone(item || blank()); dirty = false;
  $('tab-platform').hidden = !!item;
  $('editor-title').textContent = item ? '编辑 API' : '新增 API'; $('curl-text').value = ''; notice('', false, true);
  $('json-text').value = JSON.stringify(draft, null, 2);
  resetPlatform();
  try { renderForm(); displayMode(item ? 'form' : 'platform'); }
  catch (error) { displayMode('json'); notice(error.message, false, true); }
  $('editor').showModal();
}
async function closeEditor() {
  if (busy) return;
  if (dirty && !await confirmAction('放弃修改？', '尚未保存的草稿将被丢弃。', '放弃修改')) return;
  $('editor').close();
  resetPlatform();
}
function lockEditor(locked) {
  for (const control of $('editor').querySelectorAll('button,input,select,textarea')) {
    if (locked) { control.dataset.preDisabled = String(control.disabled); control.disabled = true; }
    else if (control.dataset.preDisabled !== undefined) { control.disabled = control.dataset.preDisabled === 'true'; delete control.dataset.preDisabled; }
  }
  $('editor').setAttribute('aria-busy', String(locked));
}
async function save() {
  if (busy) return;
  if (mode === 'platform') return savePlatform();
  try {
    const definition = mode === 'form' ? readForm() : readJSON();
    if (!definition.name || !definition.description || !definition.request?.url) throw new Error('请填写工具名称、用途说明和请求地址');
    lockEditor(true);
    await mutate('save', { revision: editRevision, original_name: originalName, definition });
    dirty = false; $('editor').close(); notice(`${definition.display_name || definition.name} 已保存并生效。`);
  } catch (error) { notice(error.message, true, true); }
  finally { lockEditor(false); }
}
$('save').onclick = save;
$('add').onclick = () => openEditor();
$('search').oninput = renderList;
$('method-filter').onchange = renderList;
$('refresh').onclick = () => refresh().then(() => { if (!state.error) notice('列表已刷新。'); }).catch((error) => notice(error.message, true));
$('close-editor').onclick = closeEditor; $('cancel').onclick = closeEditor;
$('editor').addEventListener('cancel', (event) => { event.preventDefault(); closeEditor(); });
$('editor').addEventListener('input', () => { dirty = true; });
$('editor').addEventListener('change', () => { dirty = true; });
for (const button of document.querySelectorAll('[data-mode]')) button.onclick = () => switchMode(button.dataset.mode);
for (const button of document.querySelectorAll('[data-add-map]')) button.onclick = () => { addMapping(`map-${button.dataset.addMap}`); dirty = true; };
$('add-param').onclick = () => { addParam(); dirty = true; };
$('body-kind').onchange = bodyVisibility;
$('detect-path').onclick = () => {
  const names = new Set([...$('params').children].map((row) => row.fields.name.value.trim()));
  for (const match of $('url').value.matchAll(/\{([A-Za-z][A-Za-z0-9_]*)\}/g)) {
    if (!names.has(match[1])) { addParam(match[1], { type: 'string', description: '路径参数' }, true); names.add(match[1]); }
    else for (const row of $('params').children) if (row.fields.name.value.trim() === match[1]) row.fields.required.checked = true;
  }
  dirty = true;
};
$('parse-curl').onclick = async () => {
  $('parse-curl').disabled = true;
  try { const result = await bridge.apiPost('import-curl', { text: $('curl-text').value }); draft = result.definition; renderForm(); displayMode('form'); dirty = true; notice('已生成草稿。请修改工具名称、填写用途，并按需将固定值改为模型参数。', false, true); }
  catch (error) { notice(error.message, true, true); }
  finally { $('parse-curl').disabled = false; }
};
function renderPlatformCards() {
  const term = $('platform-search').value.trim().toLowerCase();
  const cards = platforms.filter((p) => p.name.toLowerCase().includes(term)).map((platform) => {
    const card = el('article', undefined, 'integration-card');
    const head = el('div', undefined, 'integration-head'); head.append(el('strong', platform.name), el('span', '可接入', 'badge'));
    const body = el('div', undefined, 'integration-body'); body.append(el('p', '广告投放、收益与用户表现报表', 'muted'), el('p', `${platform.operations.length} 个操作 · 独立账户与权限`, 'hint'));
    const footer = el('div', undefined, 'integration-actions'); footer.append(actionButton('添加账户', () => {
      $('platform-select').value = platform.id; $('platform-market').hidden = true; $('platform-setup').hidden = false;
      $('platform-setup-title').textContent = platform.name; renderPlatform(); updatePlatformPreview(); displayMode('platform');
    }, 'primary'));
    card.append(head, body, footer); return card;
  });
  $('platform-cards').replaceChildren(...cards);
  if (!cards.length) $('platform-cards').append(el('p', platforms.length ? '没有匹配的平台' : '正在加载平台…', 'muted'));
}
function updatePlatformPreview() {
  const platform = platforms.find((p) => p.id === $('platform-select').value);
  $('platform-name-preview').textContent = `调用示例：${$('platform-call-name').value.trim() || 'Ninety_Report'}_${platform?.operations[0]?.id || 'operation'}`;
}
$('platform-call-name').oninput = updatePlatformPreview;
$('platform-search').oninput = renderPlatformCards;
$('back-platforms').onclick = () => { $('platform-setup').hidden = true; $('platform-market').hidden = false; displayMode('platform'); };
function resetPlatform() {
  $('platform-call-name').value = ''; $('platform-setup').hidden = true; $('platform-market').hidden = false; $('platform-search').value = '';
  renderPlatformCards();
  $('platform-token').value = ''; $('platform-name').value = '';
  renderPlatform();
}
function renderPlatform() {
  const platform = platforms.find((item) => item.id === $('platform-select').value);
  $('platform-description').textContent = platform?.description || '平台列表尚未就绪，请刷新页面重试，或使用手动填写方式。';
  $('platform-token-label').textContent = platform?.token_label || 'Token';
  $('platform-operations').replaceChildren();
  for (const operation of platform?.operations || []) {
    const row = el('div', undefined, 'operation-row');
    const control = checkbox(false); control.dataset.operationId = operation.id;
    const title = el('label', operation.title, 'check'); title.prepend(control);
    const detail = el('details'); detail.append(el('summary', '查看接口与默认字段'), el('p', `${operation.method} ${operation.path}`, 'hint'), el('p', operation.description, 'hint'));
    const link = el('a', '官方文档'); link.href = operation.documentation; link.target = '_blank'; link.rel = 'noopener noreferrer'; detail.append(link);
    row.append(title, el('span', operation.method, 'method'), detail); $('platform-operations').append(row);
  }
}
$('platform-token').oninput = () => notice('', false, true);
$('platform-select').onchange = () => { $('platform-token').value = ''; renderPlatform(); displayMode(mode); };
async function savePlatform() {
  notice('', false, true);
  const callName = $('platform-call-name').value.trim();
  if (!/^[A-Za-z][A-Za-z_]{0,39}$/.test(callName)) { notice('请填写调用名称：仅英文字母和下划线，英文开头，最多 40 位', true, true); return; }
  const token = $('platform-token').value;
  if (!token.trim()) { notice('请填写 Report Key / Token', true, true); return; }
  const enabled_operations = [...$('platform-operations').querySelectorAll('input:checked')].map((control) => control.dataset.operationId);
  lockEditor(true);
  try {
    await mutate('connect-platform', { revision: editRevision, platform_id: $('platform-select').value, call_name: callName, name: $('platform-name').value, token, enabled_operations });
    dirty = false; $('editor').close(); resetPlatform();
    notice(`平台已接入，已开启 ${enabled_operations.length} 个操作。可在「账户设置」中随时调整。`);
  } catch (error) { notice(error.message, true, true); }
  finally { lockEditor(false); }
}
async function loadPlatforms() {
  if (platformLoading) return;
  platformLoading = true; $('retry-platforms').disabled = true; $('platform-load-error').hidden = true;
  try {
    const result = await bridge.apiGet('platforms');
    platforms = result.platforms || [];
    $('platform-select').replaceChildren();
    for (const platform of platforms) { const option = el('option', platform.name); option.value = platform.id; $('platform-select').append(option); }
    renderPlatform(); renderPlatformCards();
    if (mode === 'platform') displayMode(mode);
  } catch {
    $('platform-load-error').hidden = false;
    if (!platforms.length) $('platform-cards').replaceChildren();
  } finally { platformLoading = false; $('retry-platforms').disabled = false; }
}

$('retry-platforms').onclick = loadPlatforms;

try {
  if (!bridge) throw new Error('请从 AstrBot 插件详情中的「API 管理」打开此页面。');
  await bridge.ready(); await refresh(); void loadPlatforms();
} catch (error) { notice(error.message, true); $('list').replaceChildren(); $('add').disabled = true; }
