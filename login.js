document.querySelector('#loginForm').addEventListener('submit', async (event) => {
  event.preventDefault();
  const username = document.querySelector('#username').value.trim();
  const password = document.querySelector('#password').value;
  const response = await fetch('/api/login', { method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({ username, password }) });
  const result = await response.json();
  if (!response.ok) { document.querySelector('#error').textContent = result.message; return; }
  sessionStorage.setItem('dispatchUser', JSON.stringify(result));
  location.href = result.role === 'admin' ? '/admin.html' : '/staff.html';
});
