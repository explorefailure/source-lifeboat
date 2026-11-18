"use strict";
let csrf = "";
let ownerAuthenticated = false;
const byId = (id) => document.getElementById(id);
const message = (text, error=false) => { const node=byId("message"); node.textContent=text; node.className=error ? "error" : ""; };
async function request(path, options={}) {
  options.headers = {"Content-Type":"application/json", ...(options.headers||{})};
  if (options.method && options.method !== "GET") options.headers["X-CSRF-Token"] = csrf;
  const response = await fetch(path, options); const value = await response.json();
  if (!response.ok) throw new Error(value.error || `Request failed (${response.status})`); return value;
}
function addText(parent, tag, text, className="") { const node=document.createElement(tag); node.textContent=text; if(className) node.className=className; parent.append(node); return node; }
function renderJobs(jobs) {
  const root=byId("jobs"); root.replaceChildren();
  if (!jobs.length) { addText(root,"p","No backups yet.","meta"); return; }
  for (const job of jobs) {
    const card=document.createElement("article"); card.className="job";
    addText(card,"span",job.state,"state"); addText(card,"h3",job.repository || "Unknown repository");
    if (job.progress && job.progress.message) addText(card,"p",job.progress.message,"meta");
    if (job.error) addText(card,"p",job.error,"error");
    if (job.state === "no-release") addText(card,"p","No published stable release exists; no branch fallback was used.");
    if (job.snapshot) {
      addText(card,"p",`Release ${job.snapshot.tag} · commit ${job.snapshot.commit_sha}`,"meta");
      const table=document.createElement("table"); table.className="files";
      const head=document.createElement("thead"); const hr=document.createElement("tr"); ["File","Size","SHA-256"].forEach(x=>addText(hr,"th",x)); head.append(hr); table.append(head);
      const body=document.createElement("tbody");
      for (const file of job.files || []) { const tr=document.createElement("tr"); addText(tr,"td",file.original_filename); addText(tr,"td",`${file.size} bytes`); addText(tr,"td",file.sha256,"hash"); body.append(tr); } table.append(body); card.append(table);
      if (job.publication && job.publication.forgejo_release_url) { const link=document.createElement("a"); link.href=job.publication.forgejo_release_url; link.rel="noopener noreferrer"; link.textContent="Open archived release in this app’s Forgejo"; card.append(link); }
    }
    if (["queued","resolving","downloading","verifying","staged","publishing"].includes(job.state)) { const cancel=addText(card,"button","Cancel"); cancel.addEventListener("click",async()=>{ try { await request(`/api/jobs/${job.id}/cancel`,{method:"POST",body:"{}"}); await loadJobs(); } catch(e){message(e.message,true);} }); }
    root.append(card);
  }
}
async function loadJobs() { try { const value=await request("/api/jobs"); renderJobs(value.jobs); } catch(e){message(e.message,true);} }
async function initialise() {
  try {
    const status=await request("/api/status"); csrf=status.csrf_token; ownerAuthenticated=status.authenticated;
    byId("setup").hidden=!status.setup_needed; byId("setup").textContent=status.setup_message || "";
    const settingsPage = location.pathname === "/settings";
    if(settingsPage) {
      document.title="Settings · Source Lifeboat";
      document.querySelector("h1").textContent="Settings";
      document.querySelector(".view-head .label").textContent="Your installation";
      document.querySelector(".view-head .lead").textContent="Configure the GitHub connection for this independent installation.";
    }
    byId("login-panel").hidden=!settingsPage || status.authenticated;
    byId("owner-setup-form").hidden=!status.owner_setup_needed;
    byId("login-form").hidden=status.owner_setup_needed;
    byId("login-panel").querySelector("h2").textContent=status.owner_setup_needed ? "Create your owner password" : "Unlock Settings";
    byId("owner-setup-key-label").hidden=!status.owner_setup_key_required;
    byId("owner-setup-key").required=status.owner_setup_key_required;
    byId("app-panel").hidden=settingsPage;
    byId("backup-catalog").hidden=!status.authenticated;
    byId("start-backup").textContent="Back up project";
    byId("backup-access-note").textContent=status.setup_needed ? "Complete server setup before starting a backup." : status.authenticated ? "Your backup will appear below, with download and verification progress." : "Owner access and GitHub connections are managed in Settings.";
    byId("start-backup").disabled=status.setup_needed;
    byId("settings-panel").hidden=!settingsPage;
    byId("settings-controls").hidden=!status.authenticated;
    if(!settingsPage && !byId("repository-url").value) byId("repository-url").value=sessionStorage.getItem("pending-backup-url") || "";
    document.querySelectorAll(".rail a").forEach(a=>{ if(a.getAttribute("href")===(settingsPage ? "/settings" : "/")) a.setAttribute("aria-current","page"); else a.removeAttribute("aria-current"); });
    if (status.authenticated) { if(settingsPage) { await loadGitHubSettings(); await loadDownloadSettings(); } else await loadJobs(); }
  } catch(e) { message(e.message,true); }
}
byId("login-form").addEventListener("submit",async(event)=>{ event.preventDefault(); try { await request("/api/login",{method:"POST",body:JSON.stringify({password:byId("login-token").value})}); byId("login-token").value=""; if(new URLSearchParams(location.search).get("return")==="backup") {location.assign("/");return;} await initialise(); } catch(e){message(e.message,true);} });
byId("backup-form").addEventListener("submit",async(event)=>{ event.preventDefault();
  if(!ownerAuthenticated) { sessionStorage.setItem("pending-backup-url",byId("repository-url").value); location.assign("/settings?return=backup"); return; }
  const button=byId("start-backup"); button.disabled=true; button.textContent="Checking GitHub…";
  try { await request("/api/repositories",{method:"POST",body:JSON.stringify({url:byId("repository-url").value})}); byId("repository-url").value=""; sessionStorage.removeItem("pending-backup-url"); message("Backup queued."); await loadJobs(); } catch(e){message(e.message,true);} finally {button.disabled=false;button.textContent="Back up project";} });
byId("refresh").addEventListener("click",loadJobs);
initialise();

setInterval(() => { if (ownerAuthenticated && !byId("app-panel").hidden) loadJobs(); }, 3000);

function showGitHubSettings(value) {
  byId("github-status").textContent = value.configured ? (value.kind === "oauth" ? `Connected with GitHub${value.account ? " as " + value.account : ""}.` : `Token configured (${value.source}). Test the connection to check its account and quota.`) : "Anonymous access. No GitHub token is configured.";
  byId("github-client-id").value=value.client_id || "";
  byId("github-disconnect").disabled=!value.configured;
  if(value.tested) {
    const reset=new Date(value.quota.reset*1000).toLocaleString();
    byId("github-result").textContent = `${value.account ? "Connected as " + value.account : "Anonymous connection works"}. API requests remaining: ${value.quota.remaining} / ${value.quota.limit}. Resets ${reset}.`;
  }
}
async function loadGitHubSettings() { showGitHubSettings(await request("/api/settings/github")); }
async function changeGitHub(action) {
  if(action!=="test") stopDevice();
  const input=byId("github-token"); const token=input.value; input.value="";
  const buttons=[byId("github-save"),byId("github-test"),byId("github-remove")];
  buttons.forEach(b=>b.disabled=true); byId("github-result").textContent="Checking GitHub settings…";
  try {
    const value=await request("/api/settings/github",{method:"POST",body:JSON.stringify(action==="save" ? {action,token} : {action})});
    byId("github-result").textContent=action==="remove" ? "Token removed. New backup requests will use anonymous access." : "";
    showGitHubSettings(value);
  } catch(e) { byId("github-result").textContent=e.message; }
  finally { buttons.forEach(b=>b.disabled=false); }
}
byId("github-form").addEventListener("submit",event=>{event.preventDefault();changeGitHub("save");});
byId("github-test").addEventListener("click",()=>changeGitHub("test"));
byId("github-remove").addEventListener("click",()=>changeGitHub("remove"));

let defaultDownloadFolder="";
async function loadDownloadSettings() {
  const value=await request("/api/settings/downloads");
  defaultDownloadFolder=value.default_folder;
  byId("download-folder").value=value.folder;
  byId("downloads-result").textContent=`Current folder: ${value.folder}`;
}
async function saveDownloadFolder(folder) {
  const buttons=[byId("downloads-save"),byId("downloads-reset")];
  buttons.forEach(b=>b.disabled=true);
  try {
    const value=await request("/api/settings/downloads",{method:"POST",body:JSON.stringify({folder})});
    byId("download-folder").value=value.folder;
    byId("downloads-result").textContent=`Saved. New backups will use ${value.folder}. Existing backups were not moved.`;
  } catch(e) { byId("downloads-result").textContent=e.message; }
  finally { buttons.forEach(b=>b.disabled=false); }
}
byId("downloads-form").addEventListener("submit",event=>{event.preventDefault();saveDownloadFolder(byId("download-folder").value);});
byId("downloads-reset").addEventListener("click",()=>saveDownloadFolder(defaultDownloadFolder));

let deviceTimer;
let deviceGeneration=0;
async function deviceAction(action, extra={}) {
  return request("/api/settings/github/device", {method:"POST", body:JSON.stringify({action,...extra})});
}
function stopDevice() {
  clearTimeout(deviceTimer); deviceGeneration++;
  byId("github-device").hidden=true;
  byId("github-device-code").textContent="";
  byId("github-connect").disabled=false;
}
function showDevice(value, generation) {
  if(generation!==deviceGeneration) return;
  if(value.state!=="pending") {
    stopDevice();
    const labels={connected:"GitHub connected. New backups will use this account.",denied:"GitHub sign-in was declined.",expired:"The sign-in code expired. Connect again for a new code.",failed:"GitHub sign-in failed. Connect again.",idle:"Sign-in ended. Connect again.",cancelled:"Sign-in cancelled."};
    byId("github-result").textContent=labels[value.state] || "Sign-in ended.";
    loadGitHubSettings().catch(e=>{byId("github-result").textContent=e.message;});
    return;
  }
  byId("github-device").hidden=false;
  byId("github-device-code").textContent=value.user_code;
  byId("github-device-status").textContent=`Waiting for approval. Code expires ${new Date(value.expires_at*1000).toLocaleTimeString()}.`;
  deviceTimer=setTimeout(async()=>{
    if(generation!==deviceGeneration) return;
    try { showDevice(await deviceAction("poll"),generation); }
    catch(e) { if(generation===deviceGeneration) {stopDevice();byId("github-result").textContent=e.message;} }
  },Math.max(value.interval,1)*1000);
}
byId("github-connect").addEventListener("click",async()=>{
  stopDevice(); const generation=deviceGeneration;
  byId("github-connect").disabled=true;
  byId("github-result").textContent="Requesting a sign-in code from GitHub…";
  try {const value=await deviceAction("start");if(generation===deviceGeneration){byId("github-result").textContent="";showDevice(value,generation);}}
  catch(e){if(generation===deviceGeneration){stopDevice();byId("github-result").textContent=e.message;}}
});
byId("github-device-cancel").addEventListener("click",async()=>{
  stopDevice();
  try {await deviceAction("cancel");byId("github-result").textContent="Sign-in cancelled.";}
  catch(e){byId("github-result").textContent=e.message;}
});
byId("github-disconnect").addEventListener("click",()=>{stopDevice();changeGitHub("remove");});
byId("github-client-form").addEventListener("submit",async(event)=>{
  event.preventDefault();stopDevice();
  try {await deviceAction("client",{client_id:byId("github-client-id").value.trim()});byId("github-result").textContent="Client ID saved. Connect with GitHub to sign in.";await loadGitHubSettings();}
  catch(e){byId("github-result").textContent=e.message;}
});

byId("owner-setup-form").addEventListener("submit",async(event)=>{
  event.preventDefault();const button=event.submitter;button.disabled=true;
  try {
    await request("/api/setup/owner",{method:"POST",body:JSON.stringify({password:byId("owner-password").value,confirmation:byId("owner-password-confirm").value,setup_key:byId("owner-setup-key").value})});
    byId("owner-setup-form").reset();message("Owner password created. You can now connect GitHub below.");
    if(new URLSearchParams(location.search).get("return")==="backup"){location.assign("/");return;}
    await initialise();
  }catch(e){message(e.message,true);}finally{button.disabled=false;}
});
