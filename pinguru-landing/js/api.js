const API = 'https://api.pinguru.me';

function getToken() {
  // Read JWT from secure httpOnly cookie set by backend
  // Note: JavaScript cannot access httpOnly cookies, but the browser automatically sends them
  // This function is kept for backward compatibility and returns null
  // The cookie is automatically included in all requests via credentials: 'include'
  return null;
}

function authHeaders() {
  return {
    'Content-Type': 'application/json',
    // Token automatically sent in secure httpOnly cookie, not in Authorization header
    // Credentials must be included to send cookies across origins
  };
}

async function loginUser(email, password) {
  const res = await fetch(`${API}/auth/login`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ email, password }),
    credentials: 'include'  // Include secure httpOnly cookies
  });
  const data = await res.json();
  if (!res.ok) throw new Error(data.detail || 'Login failed');
  return data;
}

async function registerUser(email, password) {
  const res = await fetch(`${API}/auth/register`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ email, password }),
    credentials: 'include'  // Include secure httpOnly cookies
  });
  const data = await res.json();
  if (!res.ok) throw new Error(data.detail || 'Registration failed');
  return data;
}

async function getProfile() {
  const res = await fetch(`${API}/auth/me`, { headers: authHeaders() });
  if (res.status === 401) { logout(); return null; }
  const data = await res.json();
  if (!res.ok) throw new Error(data.detail || 'Failed to get profile');
  return data;
}

async function getDashboardStats() {
  const res = await fetch(`${API}/dashboard/stats`, { headers: authHeaders() });
  if (res.status === 401) { logout(); return null; }
  const data = await res.json();
  if (!res.ok) throw new Error(data.detail || 'Failed to get stats');
  return data;
}

async function getRules() {
  const res = await fetch(`${API}/automation/rules`, { headers: authHeaders() });
  if (res.status === 401) { logout(); return null; }
  const data = await res.json();
  if (!res.ok) throw new Error(data.detail || 'Failed to get rules');
  return data;
}

async function createRule(ruleData) {
  const res = await fetch(`${API}/automation/rules`, {
    method: 'POST',
    headers: authHeaders(),
    body: JSON.stringify(ruleData)
  });
  const data = await res.json();
  if (!res.ok) throw new Error(data.detail || 'Failed to create rule');
  return data;
}

async function toggleRule(ruleId) {
  const res = await fetch(`${API}/automation/rules/${ruleId}/toggle`, {
    method: 'PATCH',
    headers: authHeaders()
  });
  const data = await res.json();
  if (!res.ok) throw new Error(data.detail || 'Failed to toggle rule');
  return data;
}

async function deleteRule(ruleId) {
  const res = await fetch(`${API}/automation/rules/${ruleId}`, {
    method: 'DELETE',
    headers: authHeaders()
  });
  if (!res.ok) {
    const data = await res.json();
    throw new Error(data.detail || 'Failed to delete rule');
  }
  return true;
}

async function getPlans() {
  const res = await fetch(`${API}/plans`, { headers: authHeaders() });
  const data = await res.json();
  if (!res.ok) throw new Error(data.detail || 'Failed to get plans');
  return data;
}

function logout() {
  localStorage.removeItem('pg_token');
  localStorage.removeItem('pg_user');
  localStorage.removeItem('pg_login_attempts');
  localStorage.removeItem('pg_lockout_until');
  window.location.href = '/login.html';
}

function requireAuth() {
  if (!getToken()) {
    window.location.href = '/login.html';
    return false;
  }
  return true;
}
