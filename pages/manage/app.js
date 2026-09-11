const $ = (id) => document.getElementById(id);
const clone = (value) => structuredClone(value);
const bridge = window.AstrBotPluginPage;
let state = { items: [], revision: null };
let draft = {}, originalName = null, editRevision = null, mode = 'form', dirty = false, busy = false;
let confirmResolve = null;
let platforms = [], permissionRevision = null;
const blank = () => ({ name: '', description: '', enabled: false, parameters: { type: 'object', properties: {} }, request: { method: 'GET', url: '' } });
const own = (object, key) => Object.hasOwn(object, key);
const object = (value) => value !== null && typeof value === 'object' && !Array.isArray(value);

function notice(message, error = false, editor = false) {
  const box = $(editor ? 'editor-error' : 'notice');
  box.textContent = message;
  box.classList.toggle('error', error);
  box.hidden = !message;
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
function renderList() {
  const list = $('list');
  list.replaceChildren();
  const term = $('search').value.trim().toLowerCase();
  const items = state.items.filter((item) => (!$('method-filter').value || item.request.method === $('method-filter').value) && [item.name, item.description, item.request.url].some((v) => v.toLowerCase().includes(term)));
  $('count').textContent = `${state.items.length} 个 API · ${state.items.filter((item) => item.enabled !== false).length} 个启用`;
  if (!items.length) {
    const empty = el('div', undefined, 'empty');
    empty.append(el('div', '{ }', 'symbol'), el('h2', term ? '没有匹配的 API' : '接入你的第一个 API'), el('p', term ? '试试其他名称或地址。' : '选择平台并填写 Token，或手动添加接口。', 'muted'));
    if (!term) { const add = el('button', '＋ 新增 API', 'primary'); add.onclick = () => openEditor(); empty.append(add); }
    list.append(empty); return;
  }
  for (const item of items) {
    const card = el('article', undefined, 'card');
    const top = el('div', undefined, 'card-top');
    top.append(el('span', `api_${item.name}`, 'card-title'), el('span', item.enabled === false ? '已停用' : '已启用', `badge${item.enabled === false ? ' off' : ''}`));
    const bottom = el('div', undefined, 'card-bottom');
    const endpoint = el('div', undefined, 'endpoint');
    endpoint.append(el('span', item.request.method, 'method'), document.createTextNode(item.request.url));
    const actions = el('div', undefined, 'card-actions');
    for (const [label, callback, style] of [
      ['编辑', () => openEditor(item), ''],
      [item.enabled === false ? '启用' : '停用', () => toggle(item), ''],
      ['删除', () => remove(item), 'danger'],
    ]) {
      const button = el('button', label, style); button.disabled = busy;
      button.onclick = () => Promise.resolve(callback()).catch((error) => notice(error.message, true)); actions.append(button);
    }
    bottom.append(endpoint, actions); card.append(top, el('p', item.description, 'muted'), bottom); list.append(card);
  }
}
async function mutate(endpoint, payload) {
  busy = true; renderList();
  try { state = await bridge.apiPost(endpoint, payload); renderList(); }
  finally { busy = false; renderList(); }
}
async function toggle(item) {
  await mutate('save', { revision: state.revision, original_name: item.name, definition: { ...clone(item), enabled: item.enabled === false } });
  notice(`${item.name} 已${item.enabled === false ? '启用' : '停用'}，立即生效。`);
}
async function remove(item) {
  const revision = state.revision;
  if (!await confirmAction('删除 API', `确定删除 ${item.name}？\n将移除接口定义及其工具，不会调用目标 API。`, '删除 API')) return;
  await mutate('delete', { revision, name: item.name }); notice(`${item.name} 已删除。`);
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
  for (const key of ['platform', 'form', 'curl', 'json']) { $(`panel-${key}`).hidden = key !== next; $(`tab-${key}`).setAttribute('aria-selected', String(key === next)); }
  $('save').disabled = next === 'curl' || (next === 'platform' && !platforms.some((item) => item.id === $('platform-select').value));
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
    dirty = false; $('editor').close(); notice(`${definition.name} 已保存并生效。`);
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
function resetPlatform() {
  $('platform-token').value = '';
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
  const token = $('platform-token').value;
  if (!token.trim()) { notice('请填写 Report Key / Token', true, true); return; }
  const enabled_operations = [...$('platform-operations').querySelectorAll('input:checked')].map((control) => control.dataset.operationId);
  lockEditor(true);
  try {
    await mutate('connect-platform', { revision: editRevision, platform_id: $('platform-select').value, token, enabled_operations });
    dirty = false; $('editor').close(); resetPlatform();
    notice(`平台已接入，已开启 ${enabled_operations.length} 个操作。可在「调用权限」中随时调整。`);
  } catch (error) { notice(error.message, true, true); }
  finally { lockEditor(false); }
}
function operationGroups(container, records) {
  container.replaceChildren();
  const groups = new Map();
  for (const record of records) {
    const url = record.definition?.request.url || '';
    let service;
    try { service = new URL(url).origin; } catch { service = '其他接口'; }
    const key = service + ' ' + record.method;
    if (!groups.has(key)) groups.set(key, { service, method: record.method, records: [] });
    groups.get(key).records.push(record);
  }
  for (const group of groups.values()) {
    const section = el('section', undefined, 'operation-group');
    const header = el('div', undefined, 'section-head');
    const all = checkbox(false);
    const label = labeled(`${group.service} · ${group.method}（${group.records.length} 个操作）`, all);
    label.className = 'check'; header.append(label); section.append(header);
    const checkboxes = [];
    for (const record of group.records) {
      const row = el('div', undefined, 'operation-row');
      const control = checkbox(record.definition.enabled !== false);
      control.dataset.operationName = record.definition?.name || '';
      const title = labeled(`${record.definition.name} · ${record.method} ${record.path}`, control); title.className = 'check';
      row.append(title, el('p', record.description || '', 'muted'));
      if (record.definition) {
        const details = el('details'); details.append(el('summary', '查看调用参数'));
        for (const [name, schema] of Object.entries(record.definition.parameters?.properties || {})) {
          details.append(el('p', `${name} · ${Array.isArray(schema.type) ? schema.type.join(' / ') : schema.type || '复合结构'}${record.definition.parameters.required?.includes(name) ? ' · 必填' : ' · 可选'}${Object.hasOwn(schema, 'default') ? ` · 默认值：${JSON.stringify(schema.default)}` : ''}`, 'hint'));
          if (schema.description) details.append(el('p', schema.description, 'hint'));
        }
        if (!Object.keys(record.definition.parameters?.properties || {}).length) details.append(el('p', '无需模型填写参数', 'hint'));
        row.append(details);
      }
      section.append(row); checkboxes.push(control);
    }
    const selectable = checkboxes.filter((control) => !control.disabled);
    function sync() { all.disabled = !selectable.length; all.checked = selectable.length > 0 && selectable.every((c) => c.checked); all.indeterminate = selectable.some((c) => c.checked) && !all.checked; }
    all.onchange = () => { for (const control of selectable) control.checked = all.checked; dirty = true; };
    for (const control of selectable) control.onchange = sync;
    sync(); container.append(section);
  }
}
$('permissions').onclick = () => {
  permissionRevision = state.revision; $('permissions-error').hidden = true;
  operationGroups($('permission-groups'), state.items.map((item) => ({ method: item.request.method, path: item.request.url, description: item.description, definition: item, supported: true })));
  if (!state.items.length) $('permission-groups').append(el('p', '尚未接入任何操作。请先新增 API。', 'muted'));
  $('permissions-dialog').showModal();
};
$('close-permissions').onclick = $('cancel-permissions').onclick = () => { if (!busy) $('permissions-dialog').close(); };
$('permissions-dialog').addEventListener('cancel', (event) => { if (busy) event.preventDefault(); });
$('save-permissions').onclick = async () => {
  if (busy) return;
  const enabled_names = [...$('permission-groups').querySelectorAll('input[data-operation-name]:checked')].map((input) => input.dataset.operationName);
  const controls = [...$('permissions-dialog').querySelectorAll('input,button')];
  const previous = controls.map((control) => control.disabled); controls.forEach((control) => { control.disabled = true; });
  try { await mutate('permissions', { revision: permissionRevision, enabled_names }); $('permissions-dialog').close(); notice('调用权限已保存并立即生效。'); }
  catch (error) { $('permissions-error').textContent = error.message; $('permissions-error').hidden = false; }
  finally { controls.forEach((control, index) => { control.disabled = previous[index]; }); }
};
async function loadPlatforms() {
  try {
    const result = await bridge.apiGet('platforms');
    platforms = result.platforms || [];
    $('platform-select').replaceChildren();
    for (const platform of platforms) { const option = el('option', platform.name); option.value = platform.id; $('platform-select').append(option); }
    renderPlatform();
    if (mode === 'platform') displayMode(mode);
  } catch { notice('平台列表加载失败，请刷新重试；仍可使用表单、cURL 或 JSON。', true); }
}

try {
  if (!bridge) throw new Error('请从 AstrBot 插件详情中的「API 管理」打开此页面。');
  await bridge.ready(); await refresh(); void loadPlatforms();
} catch (error) { notice(error.message, true); $('list').replaceChildren(); $('add').disabled = true; }
