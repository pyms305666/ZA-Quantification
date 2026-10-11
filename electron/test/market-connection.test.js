"use strict";
const assert = require("assert");
const fs = require("fs");
const path = require("path");
const vm = require("vm");
function harness() {
  const timers = new Map(), sockets = [], events = new Map(), messages = [];
  let count = 0, symbols = ["A"], failConstruct = false;
  const c = {console, document: {visibilityState:"visible", addEventListener:(n,f)=>events.set(n,f), removeEventListener:n=>events.delete(n)},
    addEventListener(){}, removeEventListener(){}, setTimeout:(f,d)=>{const id=++count; timers.set(id,{f,d}); return id;}, clearTimeout:id=>timers.delete(id),
    WebSocket: class {constructor(){if(failConstruct)throw Error("construction");this.readyState=0;this.sent=[];sockets.push(this);}
      open(){this.readyState=1;this.onopen?.();} close(){this.readyState=3;this.onclose?.();}
      send(x){assert.equal(this.readyState,1);this.sent.push(JSON.parse(x));} ack(x){this.onmessage?.({data:JSON.stringify(x)});}}
  };
  c.window=c; vm.createContext(c);
  vm.runInContext(fs.readFileSync(path.join(__dirname,"../../static/market-connection.js"),"utf8"),c);
  const manager=c.MarketConnection.create({url:"ws://fixture.invalid",getSymbols:()=>symbols,onMessage:x=>messages.push(x)});
  return {c,manager,sockets,timers,messages,set symbols(x){symbols=x;},set failConstruct(x){failConstruct=x;},
    visibility(v){c.document.visibilityState=v;events.get("visibilitychange")();},
    tick(delay){assert.equal(timers.size,1);const [id,t]=[...timers][0];assert.equal(t.d,delay);timers.delete(id);t.f();return t.f;}};
}
{
  const h=harness();h.manager.connect();h.manager.connect();assert.equal(h.sockets.length,1);
  for(const delay of [3000,6000,12000,24000,30000,30000]) {h.sockets.at(-1).close();h.manager.connect();h.tick(delay);}
  h.sockets.at(-1).open();h.sockets.at(-1).close();h.tick(3000);h.manager.dispose();assert.equal(h.timers.size,0);
}
{
  const h=harness();h.failConstruct=true;h.manager.connect();h.tick(3000);h.tick(6000);
  h.failConstruct=false;h.tick(12000);assert.equal(h.sockets.length,1);h.sockets[0].open();h.manager.dispose();
}
{
  const h=harness();h.manager.connect();const old=h.sockets[0];old.open();
  h.visibility("hidden");h.visibility("visible");assert.equal(h.sockets.length,1);
  const callbacks=[old.onopen,old.onclose,old.onerror,old.onmessage];h.visibility("hidden");old.close();assert.equal(h.timers.size,0);
  h.visibility("visible");h.visibility("visible");assert.equal(h.sockets.length,2);h.sockets[1].open();
  callbacks[0]();callbacks[1]();callbacks[2]();callbacks[3]({data:JSON.stringify({type:"quote"})});
  assert.equal(h.messages.length,0);assert.equal(h.timers.size,0);assert.equal(h.manager.socket,h.sockets[1]);h.manager.dispose();
}
{
  const h=harness();h.manager.connect();const ws=h.sockets[0];ws.open();
  for(const delay of [1000,2000,4000,5000,5000,5000]) {
    ws.ack({type:"subscribed",failed:[{symbol:"A"}]});ws.ack({type:"subscribed",failed:[{symbol:"A"}]});
    h.tick(delay);
  }
  ws.ack({type:"subscribed",failed:[{symbol:"A"}]});assert.equal(h.timers.size,0);assert.equal(ws.sent.length,7);
  h.symbols=["B"];h.manager.subscribe();ws.ack({type:"subscribed",failed:[{symbol:"B"}]});
  const stale=[...h.timers.values()][0].f;
  h.symbols=["C"];h.manager.subscribe();const sent=ws.sent.length;stale();assert.equal(ws.sent.length,sent);
  ws.ack({type:"subscribed",failed:[{symbol:"B"}]});assert.equal(h.timers.size,0);
  ws.ack({type:"subscribed",failed:[{symbol:"C"}]});assert.equal(h.timers.size,1);
  ws.ack({type:"subscribed",subscribed:["B"],skipped:[]});assert.equal(h.timers.size,1);
  ws.ack({type:"subscribed",subscribed:[],skipped:["C"]});assert.equal(h.timers.size,0);
  h.manager.dispose();
}
console.log("PASS market connection: one socket/timer, full backoff, construction failure, visibility, stale callbacks/ACK, capped subscription retries");
