const providerGrid = document.querySelector('#providerGrid');
const jobList = document.querySelector('#jobList');
const message = document.querySelector('#message');
const searchResults = document.querySelector('#searchResults');
const subscriptionList = document.querySelector('#subscriptionList');

const labels = {
  queued: '排队中',
  waiting_auth: '等待配置',
  completed: '已完成',
  failed: '失败',
};

async function loadProviders() {
  const response = await fetch('/api/providers');
  const providers = await response.json();
  providerGrid.innerHTML = providers.map(item => `
    <article class="provider">
      <strong>${item.label}</strong>
      <span class="state ${item.configured ? 'ready' : ''}">${item.configured ? '已连接' : '等待授权'}</span>
    </article>
  `).join('');
}

async function loadJobs() {
  const response = await fetch('/api/jobs');
  const jobs = await response.json();
  jobList.innerHTML = jobs.length ? jobs.map(job => `
    <article class="job">
      <div><strong>${escapeHtml(job.title)}</strong><p>${escapeHtml(job.detail.message || job.kind)}</p></div>
      <span class="status">${labels[job.status] || job.status}</span>
    </article>
  `).join('') : '<p class="empty">还没有任务。</p>';
}

async function loadSubscriptions() {
  const response = await fetch('/api/subscriptions');
  const items = await response.json();
  subscriptionList.innerHTML = items.length ? items.map(item => `
    <article class="job"><div><strong>${escapeHtml(item.keyword)}</strong><p>${item.last_checked_at ? '最近检查：' + escapeHtml(item.last_checked_at) : '等待首次检查'}</p></div><span class="status">追更中</span></article>
  `).join('') : '<p class="empty">还没有追更订阅。</p>';
}

function escapeHtml(value) {
  const node = document.createElement('div');
  node.textContent = value;
  return node.innerHTML;
}

document.querySelector('#intakeForm').addEventListener('submit', async event => {
  event.preventDefault();
  const form = new FormData(event.currentTarget);
  const response = await fetch('/api/intake', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify(Object.fromEntries(form.entries())),
  });
  const result = await response.json();
  message.textContent = response.ok ? '已加入队列。' : (result.detail || '提交失败');
  if (response.ok) {
    event.currentTarget.reset();
    event.currentTarget.elements.target_folder.value = '影视';
    await loadJobs();
  }
});

document.querySelector('#refreshButton').addEventListener('click', loadJobs);

document.querySelector('#searchForm').addEventListener('submit', async event => {
  event.preventDefault();
  const query = new FormData(event.currentTarget).get('q');
  searchResults.innerHTML = '<p class="empty">正在搜索…</p>';
  const response = await fetch(`/api/search?q=${encodeURIComponent(query)}`);
  const items = await response.json();
  if (!response.ok) { searchResults.innerHTML = `<p class="empty">${escapeHtml(items.detail || '搜索失败')}</p>`; return; }
  searchResults.innerHTML = items.length ? items.map(item => `
    <article class="result"><div><span class="provider-pill">${escapeHtml(item.provider_label)}</span><strong>${escapeHtml(item.title)}</strong><br><a href="${item.url}" target="_blank" rel="noreferrer">${escapeHtml(item.url)}</a></div><button type="button" data-url="${item.url}" data-title="${escapeHtml(item.title)}" class="intake-result">入库</button></article>
  `).join('') : '<p class="empty">没有找到支持网盘的结果。</p>';
});

searchResults.addEventListener('click', event => {
  const button = event.target.closest('.intake-result');
  if (!button) return;
  const form = document.querySelector('#intakeForm');
  form.elements.title.value = button.dataset.title;
  form.elements.share_url.value = button.dataset.url;
  form.scrollIntoView({behavior: 'smooth'});
});

document.querySelector('#subscriptionForm').addEventListener('submit', async event => {
  event.preventDefault();
  const keyword = new FormData(event.currentTarget).get('keyword');
  const response = await fetch('/api/subscriptions', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({keyword, auto_intake:true})});
  if (response.ok) { event.currentTarget.reset(); await loadSubscriptions(); }
});

Promise.all([loadProviders(), loadJobs(), loadSubscriptions()]).catch(error => { message.textContent = error.message; });
