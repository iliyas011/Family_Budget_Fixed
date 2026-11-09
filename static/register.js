'use strict';
const form = document.getElementById('register-form');
const button = document.getElementById('register');
const message = document.getElementById('login-message');
document.getElementById('back').addEventListener('click', () => { window.location.href = '/login'; });
form.addEventListener('submit', async (event) => {
  event.preventDefault();
  if (button.disabled) return;
  const body = Object.fromEntries(new FormData(form).entries());
  if (body.password !== body.password_repeat) {
    message.textContent = 'Passwords do not match. Please repeat the password.';
    message.className = 'error'; form.elements.password_repeat.value = ''; form.elements.password_repeat.focus();
    return;
  }
  button.disabled = true; message.textContent = 'Creating account…'; message.className = '';
  try {
    const response = await fetch('/api/register', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body)});
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || 'Registration failed.');
    form.elements.password.value = ''; form.elements.password_repeat.value = '';
    window.location.replace('/');
  } catch (error) {
    message.textContent = error instanceof TypeError ? 'Cannot connect. Start the server and try again.' : error.message;
    message.className = 'error'; button.disabled = false;
  }
});
