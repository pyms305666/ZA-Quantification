"use strict";
const assert=require("assert"),fs=require("fs"),path=require("path"),os=require("os");
const {EventEmitter}=require("events");
const b=require("../backend");
(async()=>{
  assert.equal(b.publicUrl("0.0.0.0",8123),"http://127.0.0.1:8123");
  assert.equal(b.publicUrl("::",8123),"http://[::1]:8123");
  assert.equal(b.publicUrl("[2001:db8::1]",8123),"http://[2001:db8::1]:8123");
  const directory=fs.mkdtempSync(path.join(os.tmpdir(),"za-startup-"));
  try {
    const config=path.join(directory,"custom.json");
    fs.writeFileSync(config,JSON.stringify({server:{host:"::",port:8123},tqsdk:{password:"fake pass"}}));
    const settings=b.readServerSettings(directory,{TQ_GATEWAY_CONFIG:"custom.json"});
    assert.equal(settings.configPath,config);assert.equal(settings.url,"http://[::1]:8123");
    const file=path.join(directory,"log.txt");
    for(let n=0;n<12;n++)b.appendRotating(file,b.redact(`中文 fake pass ${n}`,settings.secrets),60,3);
    assert.equal(fs.readdirSync(directory).filter(x=>x.startsWith("log.txt")).length,4);
    for(const name of fs.readdirSync(directory).filter(x=>x.startsWith("log.txt")))assert.ok(!fs.readFileSync(path.join(directory,name),"utf8").includes("fake pass"));
  }finally{fs.rmSync(directory,{recursive:true,force:true});}
  let aborted=false;
  const stalled=await b.probeStatus("http://fixture.invalid",10,async(url,{signal})=>{
    signal.addEventListener("abort",()=>aborted=true);return {ok:true,json:()=>new Promise(()=>{})};
  });
  assert.equal(stalled.running,false);assert.equal(aborted,true);
  const child=new EventEmitter();child.stdout=new EventEmitter();child.stderr=new EventEmitter();
  const result=await b.launchBackend({cmd:"missing",args:[]},{cwd:"fixture",env:{},timeout:100,
    spawn:()=>{process.nextTick(()=>child.emit("error",Error("ENOENT")));return child;}});
  assert.equal(result.error,"ENOENT");
  const late=new EventEmitter();let registered=0,killed=0;
  const timedOut=await b.launchBackend({cmd:"late",args:[]},{cwd:"fixture",env:{},timeout:5,
    spawn:()=>late,onChild:()=>registered++,stop:()=>killed++});
  assert.ok(timedOut.error);late.emit("spawn");assert.equal(registered,2);assert.equal(killed,1);
  let launched=0,stopped=0;
  const existing=await b.ensureBackend({url:"fixture",route:"C",candidates:[{cmd:"py",args:[]}],probe:async()=>({running:true,route:"C"}),launch:()=>launched++,stop:()=>stopped++});
  assert.equal(existing.ready,true);assert.equal(launched,0);assert.equal(stopped,0);
  let clock=0;
  const attempts=[],owned=[];
  const failed=await b.ensureBackend({url:"fixture",route:"C",now:()=>clock,sleep:async ms=>clock+=ms,
    candidates:[{cmd:"venv",args:[]},{cmd:"py",args:[]},{cmd:"python",args:[]}],
    probe:async(url,timeout)=>{assert.ok(timeout<=1000);clock+=timeout;return {running:false};},
    launch:async(candidate,timeout)=>{attempts.push(timeout);clock+=1000;return candidate.cmd==="venv"?{error:"ENOENT"}:{child:{exitCode:null,killed:false}};},
    stop:async child=>owned.push(child)});
  assert.equal(failed.ready,false);assert.equal(clock,30000);assert.ok(attempts[1]<attempts[0]);assert.equal(attempts.length,2);assert.equal(owned.length,1);
  assert.equal(b.escapeHtml('<script>"&'),"&lt;script&gt;&quot;&amp;");
  console.log("PASS startup: configuration/IPv6, UTF-8 rotation/redaction, stalled body timeout, asynchronous ENOENT, existing-service ownership, shared deadline, escaped diagnostics");
})().catch(error=>{console.error(error);process.exitCode=1;});
