'use strict';
const form = document.getElementById('login-form');
const button = document.getElementById('sign-in');
const message = document.getElementById('login-message');
form.addEventListener('submit', async (event) => {
  event.preventDefault();
  if (button.disabled) return;
  button.disabled = true; message.textContent = 'Signing in…'; message.className = '';
  try {
    const response = await fetch('/api/login', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(Object.fromEntries(new FormData(form).entries()))});
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || 'Sign in failed.');
    form.elements.password.value = '';
    window.location.replace('/');
  } catch (error) {
    message.textContent = error instanceof TypeError ? 'Cannot connect. Start the server and try again.' : error.message;
    message.className = 'error'; button.disabled = false;
  }
});
document.getElementById('open-register').addEventListener('click', () => { window.location.href = '/register'; });
