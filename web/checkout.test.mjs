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
  return {map,getItem(k){if(blocked)throw Error('Storage blocked');return map.get(k)??null},
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
async function page({zh=false,built=true,local=storage(),session=storage(),status='unpaid',pay='/api/orders/test-order/pay',responseToken=token,url,downloads={book:'/api/orders/test-order/manga.pdf',clip:'/manga/api/orders/test-order/manga.mp4'}}={}) {
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
  const sandbox={document:doc,location,URL:class extends URL {static createObjectURL(){return 'blob:test'}static revokeObjectURL(){}},FormData,
    matchMedia:()=>({matches:true}),localStorage:local,sessionStorage:session,
    setInterval:fn=>{intervals.set(++nextInterval,fn);return nextInterval},clearInterval:id=>intervals.delete(id),
    fetch:async(value,opts={})=>{const path=String(value);calls.push({url:path,opts});let body;
      if(path.endsWith('/config'))body={checkout_ready:true,products:['book','all'],charges:{book:{amount:'1.10',currency:'CNY'},all:{amount:'2.20',currency:'CNY'}},video_max_seconds:15,generation:'sample'};
      else if(opts.method==='POST')body={id:'test-order',buyer_token:responseToken,payment};
      else body={id:'test-order',status:currentStatus,payment,payment_test:true,downloads};
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
  assert.equal(p.local.map.get('momentBuyerToken:'+api+':test-order'),token);
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
