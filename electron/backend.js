/* =====================================================================
 * backend.js —— 后端进程管理的"可测试纯逻辑" + 日志工具
 * =====================================================================
 * 从 main.js 拆出来的原因（见 docs/项目检查报告-2026-09-11.md P1/P2）：
 *   1. main.js 直接给 portBlockedBy 赋值（隐式全局变量）→ 状态残留、无法单测；
 *   2. Python 解释器写死 "python"，依赖系统 PATH，出错时无从诊断；
 *   3. stdio:"ignore" 丢弃后端输出，启动失败只剩"30 秒超时"，定位困难。
 *
 * 本模块只放"不依赖 Electron"的部分，可以用 node 直接单测：
 *   - resolvePythonCandidates(): 生成 Python 解释器候选链（依赖注入 existsSync，可测）
 *   - createTailBuffer():       保存最近 N 行日志（错误页直接展示，不用翻文件）
 * 日志文件路径、spawn/exit/error 处理留在 main.js（需要 Electron 运行时）。
 * ===================================================================== */

const path = require("path");
const fs = require("fs");
const os = require("os");

/**
 * 生成后端启动的 Python 解释器候选链（按优先级）。
 *
 * @param {string} projectDir  项目根目录（launcher.py 所在）
 * @param {string} launcherPath launcher.py 绝对路径
 * @param {(p: string) => boolean} existsSync 依赖注入：文件存在性检查（测试可传假函数）
 * @returns {{cmd: string, args: string[], source: string}[]}
 *          候选数组；spawn 时逐个尝试，ENOENT（找不到命令）就换下一个
 */
function resolvePythonCandidates(projectDir, launcherPath, existsSync = fs.existsSync) {
  const candidates = [];
  // ① 项目虚拟环境（最可靠：依赖一定装在这里，不依赖系统任何配置）
  const venvPython = path.join(projectDir, ".venv", "Scripts", "python.exe");
  if (existsSync(venvPython)) {
    candidates.push({ cmd: venvPython, args: [launcherPath], source: "venv" });
  }
  // ② Windows 官方安装包自带的 py 启动器：py -3 显式选 Python 3
  candidates.push({ cmd: "py", args: ["-3", launcherPath], source: "py-launcher" });
  // ③ 系统 PATH 里的 python（最后的兜底）
  candidates.push({ cmd: "python", args: [launcherPath], source: "path" });
  return candidates;
}

/**
 * 最近 N 行日志缓冲：错误页直接展示尾部内容，用户不必去翻日志文件。
 *
 * @param {number} capacity 最多保留多少行（超出丢最旧的）
 * @returns {{push(line: string), text(): string, clear()}}
 */
function createTailBuffer(capacity = 40) {
  const lines = [];
  return {
    push(line) {
      lines.push(line);
      while (lines.length > capacity) lines.shift();
    },
    text() {
      return lines.join("\n");
    },
    clear() {
      lines.length = 0;
    },
  };
}


function publicUrl(host, port) {
  host = String(host).trim().replace(/^\[|\]$/g, "");
  host = ({"0.0.0.0":"127.0.0.1", "::":"::1"})[host] || host;
  return new URL(`http://${host.includes(":") ? `[${host}]` : host}:${port}`).origin;
}
function readServerSettings(projectDir, env = process.env) {
  let configPath = env.TQ_GATEWAY_CONFIG || "config.json";
  if (/^~[\\/]/.test(configPath)) configPath = path.join(os.homedir(), configPath.slice(2));
  configPath = path.resolve(projectDir, configPath);
  const raw = fs.existsSync(configPath) ? JSON.parse(fs.readFileSync(configPath,"utf8")) : {};
  const server = raw.server || {};
  return {configPath, url:publicUrl(server.host ?? "127.0.0.1", Number(server.port ?? 8000)),
    secrets:[raw.tqsdk?.account, raw.tqsdk?.password, env.TQ_ACCOUNT, env.TQ_PASSWORD].filter(Boolean)};
}
function redact(line, secrets = []) {
  line = String(line);
  const values = [...new Set(secrets.flatMap(x => [String(x), JSON.stringify(String(x)).slice(1,-1)]))].sort((a,b)=>b.length-a.length);
  for (const secret of values) if(secret) line = line.split(secret).join("[REDACTED]");
  line = line.replace(/(bearer\s+)\S+/gi,"$1[REDACTED]")
    .replace(/((?:password|access_token|refresh_token|token|account)["']?\s*[:=]\s*["']?)[^\s,"'}]+/gi,"$1[REDACTED]");
  if (/["'](?:quotes|bid|ask)["']\s*:/.test(line)) return "[行情内容已省略]";
  return line.slice(0,8192);
}
function appendRotating(file, line, maxBytes = 5 * 1024 * 1024, backups = 3) {
  fs.mkdirSync(path.dirname(file), {recursive:true});
  const data = line + "\n";
  if (fs.existsSync(file) && fs.statSync(file).size + Buffer.byteLength(data,"utf8") > maxBytes) {
    if(fs.existsSync(`${file}.${backups}`)) fs.unlinkSync(`${file}.${backups}`);
    for(let n=backups-1;n>=1;n--) if(fs.existsSync(`${file}.${n}`)) fs.renameSync(`${file}.${n}`,`${file}.${n+1}`);
    fs.renameSync(file,`${file}.1`);
  }
  fs.appendFileSync(file,data,"utf8");
}
async function probeStatus(url, timeout = 1000, fetchImpl = fetch) {
  const controller = new AbortController();
  let timer;
  const expired = new Promise(resolve => {timer = setTimeout(()=>{controller.abort();resolve({running:false,route:null});},timeout);});
  try {
    return await Promise.race([expired,(async()=>{
      const response = await fetchImpl(url + "/api/v1/status",{signal:controller.signal});
      if(!response.ok) return {running:false,route:null};
      const data = await response.json();
      return {running:true,route:data.route || "未知版本"};
    })()]);
  } catch {return {running:false,route:null};}
  finally {clearTimeout(timer);}
}
function launchBackend(candidate,{spawn,cwd,env,timeout,log=()=>{},onChild=()=>{},stop=child=>child.kill()}) {
  return new Promise(resolve=>{
    let child, settled=false;
    const finish=result=>{if(!settled){settled=true;clearTimeout(timer);resolve(result);}};
    const timer=setTimeout(()=>finish({child,error:"解释器启动超时"}),timeout);
    try {
      child=spawn(candidate.cmd,candidate.args,{cwd,env,windowsHide:true,stdio:["ignore","pipe","pipe"]});
      onChild(child);
      child.once("spawn",()=>{if(settled){onChild(child);stop(child);return;}finish({child,error:null});});
      child.once("error",error=>{log(`进程错误：${error.message}`);finish({child,error:error.message});});
      for(const stream of [child.stdout,child.stderr]) stream?.on("data",data=>String(data).split(/\r?\n/).filter(Boolean).forEach(log));
      child.on("exit",(code,signal)=>log(`后端退出：code=${code} signal=${signal || "-"}`));
    } catch(error){finish({child,error:error.message});}
  });
}
async function ensureBackend({url,route,candidates,launch,stop,probe=probeStatus,
  now=Date.now,sleep=ms=>new Promise(resolve=>setTimeout(resolve,ms)),log=()=>{},budget=30000}) {
  const deadline=now()+budget;
  let interpreter="",error="没有可用解释器";
  const check=()=>probe(url,Math.max(1,Math.min(1000,deadline-now())));
  const existing=await check();
  if(existing.running) return {ready:existing.route===route,blocked:existing.route===route?null:existing.route,interpreter:"已有服务",child:null};
  for(const candidate of candidates) {
    if(now()>=deadline)break;
    interpreter=[candidate.cmd,...candidate.args].join(" ");log(`尝试解释器：${interpreter}`);
    const started=await launch(candidate,deadline-now()),child=started.child;
    if(started.error){error=started.error;if(child)await stop(child);continue;}
    while(now()<deadline && child.exitCode===null && !child.killed) {
      const status=await check();
      if(status.running){
        if(status.route===route)return {ready:true,child,interpreter};
        await stop(child);return {ready:false,child:null,interpreter,blocked:status.route};
      }
      if(now()<deadline)await sleep(Math.min(500,deadline-now()));
    }
    await stop(child);error=child.exitCode!==null?`解释器退出：${child.exitCode}`:"共享 30 秒启动期限已到";
  }
  return {ready:false,child:null,interpreter,error};
}
function escapeHtml(text) {
  return String(text).replace(/[&<>"']/g,x=>({"&":"&amp;","<":"&lt;",">":"&gt;","\"":"&quot;","'":"&#39;"})[x]);
}
module.exports={resolvePythonCandidates,createTailBuffer,publicUrl,readServerSettings,redact,appendRotating,probeStatus,launchBackend,ensureBackend,escapeHtml};
