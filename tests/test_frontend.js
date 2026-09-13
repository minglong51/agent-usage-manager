const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const html = fs.readFileSync(path.join(__dirname, '../agent_usage_manager/static/index.html'), 'utf8');
const script = html.match(/<script>([\s\S]*?)<\/script>/)[1];
const source = script.slice(0, script.lastIndexOf('renderInspector();\nsetInterval('));
const agent = {pid:70001, create_time:1000.123456, runtime:'codex', label:'codex',
  project:'test-project', protected:false, supervised:null, cpu_percent:4, mem_mb:32,
  child_count:0, uptime_s:30, trend:[], cmdline:'codex', status:'running'};

function harness(fetch) {
  const nodes = new Map();
  const storage = new Map([['killToken', 'disposable-test-token']]);
  let now = 1000000;
  const classes = new Set();
  const classList = {contains:name=>classes.has(name), add:name=>classes.add(name),
    remove:name=>classes.delete(name), toggle:(name,on)=>on?classes.add(name):classes.delete(name)};
  const node = key => {
    if (!nodes.has(key)) nodes.set(key, {
      textContent:'', innerHTML:'', disabled:false, open:false, value:'', style:{}, dataset:{},
      options:[], classList, isConnected:true, addEventListener(){}, setAttribute(){},
      querySelector(){return null;}, querySelectorAll(){return [];}, contains(){return false;}, appendChild(){}, remove(){},
      close(){this.open=false;}, showModal(){this.open=true;},
    });
    return nodes.get(key);
  };
  class Clock extends Date {
    static now(){return now;}
  }
  const window = {getSelection:()=>''};
  window.self = window.top = window;
  const context = vm.createContext({
    document:{querySelector:node, createElement:node, body:{classList}}, window, Date:Clock,
    fetch, AbortController, setTimeout:()=>1, clearTimeout(){}, setInterval(){},
    matchMedia:()=>({matches:false}), navigator:{}, prompt:()=>'',
    localStorage:{getItem:key=>storage.get(key), setItem:(key,value)=>storage.set(key,value),
      removeItem:key=>storage.delete(key)},
  });
  vm.runInContext(source, context);
  context.fixture = agent;
  vm.runInContext('data={agents:[fixture],sample_age_s:0,ts:1000,cpu_count:10}; lastFetchOk=Date.now(); selectedKey=keyOf(fixture); dialogSnap={...fixture,key:keyOf(fixture)};', context);
  node('#confirm').open = true;
  return {run:code=>vm.runInContext(code,context), node, storage, context, advance:ms=>{now+=ms;}};
}

test('an open confirmation disables immediately when its sample ages', () => {
  const h = harness(()=>{throw new Error('unexpected request');});
  h.advance(11000);
  h.run('renderStale()');
  assert.equal(h.node('#review-stop').disabled, true);
  assert.equal(h.node('#do-stop').disabled, true);
  assert.equal(h.node('#do-force').disabled, true);
  assert.match(h.node('#stop-error').textContent, /stale/);
  h.run('lastFetchOk=Date.now(); renderStale()');
  assert.equal(h.node('#do-stop').disabled, false);
});

test('reusing a PID never retargets an open confirmation', async () => {
  let calls = 0;
  const h = harness(()=>{calls++;});
  h.run('data.agents=[{...fixture,create_time:2000}]; renderStale()');
  assert.equal(h.node('#do-stop').disabled, true);
  await h.run('executeStop()');
  assert.equal(calls, 0);
  assert.equal(h.run('dialogSnap.create_time'), agent.create_time);
});

test('time spent entering a token cannot bypass sample freshness', async () => {
  let calls = 0;
  const h = harness(()=>{calls++;});
  h.storage.clear();
  h.context.prompt = () => {h.advance(11000); return 'disposable-test-token';};
  await h.run('executeStop()');
  assert.equal(calls, 0);
  assert.match(h.node('#stop-error').textContent, /stale/);
});

test('concurrent refresh callers share one HTTP request', async () => {
  let calls = 0, resolve;
  const h = harness(()=>{calls++; return new Promise(done=>{resolve=done;});});
  const first = h.run('refreshNow()');
  const second = h.run('refreshNow()');
  assert.equal(calls, 1);
  resolve({ok:true,json:async()=>({agents:[],sample_age_s:0,ts:1000})});
  await Promise.all([first,second]);
  assert.equal(calls, 1);
});

test('the first-stop prompt identifies the server and its token file', async () => {
  const h = harness(async()=>({ok:true,json:async()=>({agents:[agent],sample_age_s:0,
    ts:1000,host:'qa-host.local',token_path:'/tmp/aum-qa/token'})}));
  await h.run('refreshNow()');
  h.storage.clear();
  let message;
  h.context.prompt = text => {message=text;return '';};
  await h.run('executeStop()');
  assert.match(message, /\/tmp\/aum-qa\/token/);
  assert.match(message, /on qa-host\.local/);
});

test('stop submits the captured identity once and preserves truthful completion', async () => {
  const requests = [];
  let finish;
  const h = harness((url,options)=>{
    requests.push({url,options});
    if (options?.method === 'POST') return new Promise(resolve=>{finish=resolve;});
    return Promise.resolve({ok:true,json:async()=>({agents:[],sample_age_s:0,ts:1000})});
  });
  const first = h.run('executeStop()');
  await h.run('executeStop()');
  assert.equal(requests.length, 1);
  assert.match(requests[0].url, /create_time=1000\.123456/);
  assert.match(requests[0].url, /force=false/);
  assert.equal(requests[0].options.headers['X-Kill-Token'], 'disposable-test-token');
  finish({ok:true,status:200,json:async()=>({result:'terminated',killed:1,still_running:0})});
  await first;
  assert.equal(h.node('#confirm').open, false);
  assert.match(h.node('#result').textContent, /1 process stopped/);
});

test('host markup is escaped before insertion', () => {
  const h = harness(()=>{});
  h.context.untrusted = '<img src=x onerror="alert(1)"> & \'quoted\'';
  const escaped = h.run('esc(untrusted)');
  assert.equal(escaped.includes('<'), false);
  assert.match(escaped, /&lt;img/);
  assert.match(escaped, /&quot;/);
  assert.match(escaped, /&#39;/);
});

function panelFixture(h, control='summary') {
  const document = h.context.document;
  const generation = saved => {
    const detail = {dataset:{fold:'command'},open:saved};
    const summary = {tagName:'SUMMARY',parentElement:detail,
      focus(options){document.activeElement=this;this.focusOptions=options;}};
    const button = {id:'copy-cmd',tagName:'BUTTON',disabled:false,
      focus(options){document.activeElement=this;this.focusOptions=options;}};
    return {detail,summary,button,command:{scrollTop:saved?84:0,scrollLeft:saved?12:0}};
  };
  const before = generation(true), after = generation(false);
  let current = before;
  document.activeElement = before[control];
  const panel = {
    contains:node=>Object.values(current).includes(node),
    querySelectorAll:()=>[current.detail],
    querySelector:selector=>({'.cmd-view':current.command,'#copy-cmd':current.button,
      'details[data-fold="command"] > summary':current.summary})[selector]||null,
    set innerHTML(value){current=after;document.activeElement=document.body;},
  };
  h.context.panel = panel;
  return {before,after,document};
}

test('refresh preserves a disclosure, keyboard focus, and command scroll position', () => {
  const h = harness(()=>{});
  const {after,document} = panelFixture(h);
  h.run("replacePanel(panel, 'updated markup')");
  assert.equal(after.detail.open, true);
  assert.equal(document.activeElement, after.summary);
  assert.equal(after.summary.focusOptions.preventScroll, true);
  assert.equal(after.command.scrollTop, 84);
  assert.equal(after.command.scrollLeft, 12);
});

test('refresh keeps focus on the corresponding action button', () => {
  const h = harness(()=>{});
  const {after,document} = panelFixture(h, 'button');
  h.run("replacePanel(panel, 'updated markup')");
  assert.equal(document.activeElement, after.button);
  assert.equal(after.button.focusOptions.preventScroll, true);
});

test('selecting another process does not carry over the previous reading position', () => {
  const h = harness(()=>{});
  const {after,document} = panelFixture(h);
  h.run("replacePanel(panel, 'different process', false)");
  assert.equal(after.detail.open, false);
  assert.equal(document.activeElement, document.body);
  assert.equal(after.command.scrollTop, 0);
  assert.equal(after.command.scrollLeft, 0);
});
