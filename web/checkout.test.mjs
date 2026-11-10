// Offline checkout tests: execute the actual page script, with no network or model calls.
// Run after scripts/build_site.sh: node --test web/checkout.test.mjs
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import vm from 'node:vm';
import test from 'node:test';

const server='https://marine-api.tunnel.tungo.cn/manga';
const api=server+'/api/';
const token='test-buyer+/=token';
function storage(map=new Map(), blocked=false) {
  return {map,get length(){if(blocked)throw Error('Storage blocked');return map.size},key(i){if(blocked)throw Error('Storage blocked');return [...map.keys()][i]??null},
    getItem(k){if(blocked)throw Error('Storage blocked');return map.get(k)??null},
    setItem(k,v){if(blocked)throw Error('Storage blocked');map.set(k,String(v))},
    removeItem(k){if(blocked)throw Error('Storage blocked');map.delete(k)}};
}
class Element {
  constructor(){this.dataset={};this.style={};this.children=[];this.attributes={};this.files=[];this.hidden=false;this.textContent='';this.classes=new Set();this.classList={add:c=>this.classes.add(c),remove:c=>this.classes.delete(c),contains:c=>this.classes.has(c)};}
  addEventListener(){} setAttribute(k,v){this.attributes[k]=v}
  removeAttribute(k){delete this[k]} appendChild(e){this.children.push(e)}
  replaceChildren(...children){this.children=children} pause(){} load(){} scrollIntoView(){}
}
const flush=async()=>{for(let i=0;i<5;i++)await new Promise(setImmediate)};
async function page({zh=false,built=true,local=storage(),session=storage(),status='unpaid',pay='/api/orders/test-order/pay',responseToken=token,url,config={},extra={},downloads={book:'/api/orders/test-order/manga.pdf',clip:'/manga/api/orders/test-order/manga.mp4'}}={}) {
  const html=readFileSync(built?(zh?'_site/zh/index.html':'_site/index.html'):'web/index.html','utf8');
  const elements=new Map([...html.matchAll(/\bid="([^"]+)"/g)].map(m=>[m[1],new Element()]));
  const get=id=>{assert.ok(elements.has(id),'Real page has element '+id);return elements.get(id)};
  const buttons=['book','all'].map(product=>Object.assign(new Element(),{dataset:{product}}));
  const calls=[],intervals=new Map();let nextInterval=0,currentStatus=status;
  const payment={pay_url:pay,charge:{amount:'1.10',currency:'CNY'}};
  const location=new URL(url??(built?'https://marinelj.github.io/moment-manga/':'http://localhost:18081/manga/')+(zh?'zh/':''));
  const doc={documentElement:{lang:zh?'zh-CN':'en'},getElementById:get,
    querySelectorAll:q=>q==='.buy'?buttons:[],
    querySelector:q=>q==='.buy[data-product="all"]'?buttons[1]:{content:html.match(/name="moment-manga-api-base" content="([^"]*)"/)[1]},
    createElement:()=>new Element()};
  const sandbox={document:doc,location,navigator:{clipboard:{writeText:async()=>{}}},URL:class extends URL {static createObjectURL(){return 'blob:test'}static revokeObjectURL(){}},FormData,
    matchMedia:()=>({matches:true}),localStorage:local,sessionStorage:session,
    setInterval:fn=>{intervals.set(++nextInterval,fn);return nextInterval},clearInterval:id=>intervals.delete(id),
    fetch:async(value,opts={})=>{const path=String(value);calls.push({url:path,opts});let body;
      if(path.endsWith('/config'))body={checkout_ready:true,products:['book','all'],charges:{book:{amount:'1.10',currency:'CNY'},all:{amount:'2.20',currency:'CNY'}},video_max_seconds:15,generation:'sample',...config};
      else if(opts.method==='POST')body={id:'test-order',buyer_token:responseToken,payment};
      else body={id:'test-order',status:currentStatus,payment,payment_test:true,downloads,...extra};
      return {ok:true,json:async()=>body};}};
  vm.runInNewContext(html.match(/<script>([\s\S]*?)<\/script>/)[1],sandbox);
  await flush();
  return {get,buttons,calls,local,session,setStatus:s=>{currentStatus=s},poll:async()=>{for(const fn of [...intervals.values()])await fn();await flush()},
    buy:async(product='book')=>{const file=new Blob(['fake clip'],{type:'video/mp4'});file.name='clip.mp4';get('videoInput').files=[file];get('videoInput').onchange();get('preview').duration=12;get('preview').onloadedmetadata();await buttons.find(b=>b.dataset.product===product).onclick();await flush()}};
}

for(const zh of [false,true])test(`${zh?'Chinese':'English'} Pages checkout persists buyer access through payment, fulfillment, and reload`,async()=>{
  const p=await page({zh});assert.equal(p.calls[0].url,api+'config');
  await p.buy(zh?'all':'book');
  const post=p.calls.find(c=>c.opts.method==='POST');assert.equal(post.url,api+'orders');
  assert.deepEqual([...post.opts.body.keys()],['video','product','lang']);
  assert.equal(post.opts.body.get('lang'),zh?'zh':'en');assert.equal(post.opts.body.get('product'),zh?'all':'book');
  assert.equal(p.local.map.size,0,'nothing about the order outlives this tab');assert.equal(JSON.parse(p.session.map.get('momentOrder')).buyer_token,token);
  const pay=new URL(p.get('paymentLink').href);assert.equal(pay.origin+pay.pathname,api+'orders/test-order/pay');assert.equal(pay.searchParams.get('t'),token);
  assert.equal(p.calls.at(-1).opts.headers['X-Buyer-Token'],token);
  p.setStatus('done');await p.poll();assert.equal(p.get('checkout').dataset.state,'done');
  assert.equal(p.get('orderTestNote').hidden,false);
  for(const a of p.get('downloads').children){const u=new URL(a.href);assert.equal(u.origin+u.pathname,api+'orders/test-order/'+(a.href.includes('.pdf')?'manga.pdf':'manga.mp4'));assert.equal(u.searchParams.get('t'),token)}
  assert.equal(p.get('downloads').children.length,2);assert.equal(new URL(p.get('completedClip').src).searchParams.get('t'),token);
  const restored=await page({zh:!zh,local:p.local,session:p.session,status:'done'});
  assert.equal(restored.calls.at(-1).opts.headers['X-Buyer-Token'],token);assert.equal(restored.get('downloads').children.length,2);
});

test('Same-origin backend pages retain their mount and work with cookies on older servers',async()=>{
  for(const zh of [false,true]){const p=await page({built:false,zh,responseToken:null});await p.buy();assert.equal(p.calls[0].url,'http://localhost:18081/manga/api/config');assert.equal(p.get('paymentLink').href,'http://localhost:18081/manga/api/orders/test-order/pay');assert.deepEqual(Object.keys(p.calls.at(-1).opts.headers),[])}
});

test('Already-mounted and absolute payment URLs retain queries without duplicate prefixes',async()=>{
  for(const pay of ['/manga/api/orders/test-order/pay?from=test',api+'orders/test-order/pay?from=test']){const p=await page({pay});await p.buy();const u=new URL(p.get('paymentLink').href);assert.equal(u.pathname,'/manga/api/orders/test-order/pay');assert.equal(u.searchParams.get('from'),'test');assert.equal(u.searchParams.get('t'),token)}
});

test('External payment URLs receive no buyer token; external downloads are rejected',async()=>{
  const p=await page({pay:'https://openapi.alipay.com/gateway.do?checkout=example',downloads:{book:'https://untrusted.example/manga/api/file.pdf',clip:server+'/apiary/file.mp4'}});await p.buy();assert.equal(new URL(p.get('paymentLink').href).searchParams.has('t'),false);
  p.setStatus('done');await p.poll();assert.equal(p.get('downloads').children.length,0);assert.equal(p.get('completedClip').src,undefined);
  // A different API selection must not resume an order saved for the public gateway.
  const local=await page({built:false,local:p.local,session:p.session});assert.equal(local.calls.length,1);
});

test('Blocked storage does not break a new order or its in-memory buyer authentication',async()=>{
  const p=await page({local:storage(new Map(),true),session:storage(new Map(),true)});await p.buy();assert.equal(p.get('checkout').dataset.state,'payment');assert.equal(p.calls.at(-1).opts.headers['X-Buyer-Token'],token);
});

const batchLive={generation:'live',api_style:'batch',batch_max_wait_hours:24,download_days:7,ready_email:true};

for(const zh of [false,true])test(`${zh?'Chinese':'English'} batch order: wait notice, optional email, order link, queue note and download date`,async()=>{
  const p=await page({zh,config:batchLive,extra:{payment_test:false,api_style:'batch',progress:20,files_expire_at:1794816000}});
  assert.equal(p.get('batchNote').hidden,false);assert.match(p.get('batchNote').textContent,/24/);
  assert.equal(p.get('periodNote').hidden,false);assert.match(p.get('periodNote').textContent,/7/);
  assert.equal(p.get('emailRow').hidden,false);
  p.get('emailInput').value='bad address';await p.buy();
  assert.equal(p.calls.some(c=>c.opts.method==='POST'),false,'an invalid email stops checkout');assert.ok(p.get('emailError').textContent);
  p.get('emailInput').value=' buyer@example.com ';await p.buy();
  const post=p.calls.find(c=>c.opts.method==='POST');assert.equal(post.opts.body.get('email'),'buyer@example.com');
  assert.equal(p.get('orderLinkBox').hidden,false);
  const link=new URL(p.get('orderLinkInput').value);
  assert.equal(link.origin+link.pathname,'https://marinelj.github.io/moment-manga/'+(zh?'zh/':''));
  assert.equal(link.searchParams.get('order'),'test-order');assert.equal(link.searchParams.get('t'),token);
  assert.equal(p.local.map.size,0,'the next visit in this browser must not reopen the order');
  p.setStatus('processing');await p.poll();
  assert.equal(p.get('progressNote').hidden,false);assert.match(p.get('progressNote').textContent,/24/);
  p.setStatus('done');await p.poll();
  assert.equal(p.get('expiryNote').hidden,false);assert.match(p.get('expiryNote').textContent,/2026/);
});

test('Without a mail server there is no email field and the order form stays as before',async()=>{
  const p=await page({config:{...batchLive,ready_email:false}});
  assert.equal(p.get('emailRow').hidden,true);assert.match(p.get('batchNote').textContent,/order link/);
  await p.buy();assert.deepEqual([...p.calls.find(c=>c.opts.method==='POST').opts.body.keys()],['video','product','lang']);
});

test('Standard orders show no batch notice',async()=>{
  const p=await page({config:{...batchLive,api_style:'standard'}});assert.equal(p.get('batchNote').hidden,true);
});

test('An order link opens that order on a fresh browser',async()=>{
  const p=await page({url:'https://marinelj.github.io/moment-manga/?order=test-order&t=link-token',status:'done',extra:{files_expire_at:1794816000}});
  const poll=p.calls.find(c=>c.url===api+'orders/test-order');assert.ok(poll,'polls the linked order');
  assert.equal(poll.opts.headers['X-Buyer-Token'],'link-token');
  assert.equal(p.local.map.size,0,'the link token stays out of localStorage');
  assert.equal(p.get('downloads').children.length,2);
  for(const a of p.get('downloads').children)assert.equal(new URL(a.href).searchParams.get('t'),'link-token');
});

test('A new visit shows no order or files, even after an order finished in this browser',async()=>{
  const p=await page({status:'done'});await p.buy();await p.poll();assert.equal(p.get('checkout').dataset.state,'done');
  const fresh=await page({local:p.local,status:'done'});
  assert.deepEqual(fresh.calls.map(c=>c.url),[api+'config'],'no order is looked up');
  assert.equal(fresh.get('checkout').dataset.state,undefined);assert.equal(fresh.get('orderLinkInput').value,undefined);assert.equal(fresh.get('downloads').children.length,0);
});

test('A new visit clears the order an older page left in localStorage and does not open it',async()=>{
  const local=storage(new Map([['momentOrder',JSON.stringify({id:'test-order',product:'book',api})],['momentBuyerToken:'+api+':test-order',token],['unrelated','kept']]));
  const p=await page({local,status:'done'});
  assert.deepEqual(p.calls.map(c=>c.url),[api+'config'],'no order is looked up');
  assert.equal(p.get('downloads').children.length,0);
  assert.deepEqual([...local.map.keys()],['unrelated'],'old order and token removed, other keys untouched');
  // A tab left open across the update holds an old-format record whose token was in localStorage: don't reopen it.
  const stale=await page({session:storage(new Map([['momentOrder',JSON.stringify({id:'test-order',product:'book',api})]])),status:'done'});
  assert.deepEqual(stale.calls.map(c=>c.url),[api+'config']);
});

test('Expired files show that they were deleted and offer no downloads',async()=>{
  const p=await page({url:'https://marinelj.github.io/moment-manga/?order=test-order&t=link-token',status:'done',extra:{files_expired:true,downloads:undefined}});
  assert.equal(p.get('checkout').dataset.state,'failed');assert.match(p.get('checkoutTitle').textContent,/deleted/);
  assert.equal(p.get('downloads').children.length,0);
});
