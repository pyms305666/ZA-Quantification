"use strict";
const {app,BrowserWindow}=require("electron");
const path=require("path");
const {spawn}=require("child_process");
const backend=require("./backend");
const PROJECT_DIR=path.join(__dirname,"..");
const LAUNCHER=path.join(PROJECT_DIR,"launcher.py");
const ROUTE="C 直连版";
const owned=new Set(),tail=backend.createTailBuffer(40);
let settings,logPath,secrets=[];
function log(line) {
  const safe=backend.redact(`[${new Date().toISOString()}] ${line}`,secrets);
  tail.push(safe);console.log(safe);
  try{backend.appendRotating(logPath,safe);}catch{}
}
function stopOwned(child) {
  if(!owned.has(child))return;
  owned.delete(child);
  if(child.exitCode!==null || !child.pid)return;
  if(process.platform==="win32") {
    // py 启动器可能有 Python 子进程，只清理本次 spawn 的 PID 树。
    const killer=spawn("taskkill",["/PID",String(child.pid),"/T","/F"],{windowsHide:true,stdio:"ignore"});
    killer.on("error",()=>{try{child.kill();}catch{}});
    return new Promise(resolve=>{
      const timer=setTimeout(()=>{try{killer.kill();child.kill();}catch{}resolve();},1000);
      killer.once("exit",()=>{clearTimeout(timer);resolve();});
      killer.once("error",()=>{clearTimeout(timer);resolve();});
    });
  }
  try{child.kill();}catch{}
}
app.whenReady().then(async()=>{
  logPath=path.join(process.env.LOCALAPPDATA?path.join(process.env.LOCALAPPDATA,"ZAQuant"):app.getPath("userData"),"logs","electron-backend.log");
  let result;
  try {
    settings=backend.readServerSettings(PROJECT_DIR);secrets=settings.secrets;
    log(`服务地址：${settings.url}；配置：${settings.configPath}`);
    result=await backend.ensureBackend({url:settings.url,route:ROUTE,
      candidates:backend.resolvePythonCandidates(PROJECT_DIR,LAUNCHER),log,stop:stopOwned,
      launch:(candidate,timeout)=>backend.launchBackend(candidate,{spawn,cwd:PROJECT_DIR,
        env:{...process.env,TQ_GATEWAY_CONFIG:settings.configPath,TQ_GATEWAY_NO_BROWSER:"1",PYTHONUTF8:"1"},
        timeout,log,onChild:child=>owned.add(child),stop:stopOwned})});
  }catch(error){result={ready:false,error:backend.redact(error.message,secrets)};log(result.error);}
  const win=new BrowserWindow({width:1440,height:900,minWidth:1180,minHeight:760,title:"ZA量化",
    icon:path.join(PROJECT_DIR,"assets","icon.png"),autoHideMenuBar:true,
    webPreferences:{nodeIntegration:false,contextIsolation:true}});
  if(result.ready)await win.loadURL(settings.url);
  else {
    const e=backend.escapeHtml;
    await win.loadURL("data:text/html;charset=utf-8,"+encodeURIComponent(`<!doctype html><meta charset="utf-8">
      <body style="font-family:Microsoft YaHei UI;padding:40px;color:#555"><h2>ZA量化 · ${result.blocked?"服务地址被占用":"后端启动失败"}</h2>
      <p>地址：${e(settings?.url || "配置解析失败")}</p><p>解释器：${e(result.interpreter || "未启动")}</p>
      <p>原因：${e(result.blocked?`已有路线 ${result.blocked}，本次需要 ${ROUTE}`:result.error || "启动未完成")}</p>
      <p>日志：${e(logPath)}</p><pre style="white-space:pre-wrap">${e(tail.text())}</pre></body>`));
  }
  win.on("page-title-updated",event=>{event.preventDefault();win.setTitle("ZA量化");});
}).catch(error=>{log(error.message);for(const child of [...owned])stopOwned(child);app.quit();});
app.on("window-all-closed",()=>{if(process.platform!=="darwin")app.quit();});
app.on("before-quit",()=>{for(const child of [...owned])stopOwned(child);});
