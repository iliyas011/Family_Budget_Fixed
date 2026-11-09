'use strict';
const requestForm = document.getElementById('request-form');
const resetForm = document.getElementById('reset-form');
const message = document.getElementById('message');
let challenge = '';
let requesting = false;
async function post(path, body) {
  const response = await fetch(path, {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body)});
  const data = await response.json();
  if (!response.ok) throw new Error(data.error || 'Request failed.');
  return data;
}
requestForm.addEventListener('submit', async (event) => {
  event.preventDefault();
  if (requesting) return;
  requesting = true;
  document.getElementById('request-code').disabled = true;
  try {
    const data = await post('/api/password/request', Object.fromEntries(new FormData(requestForm)));
    challenge = data.challenge;
    resetForm.hidden = false;
    message.textContent = data.message;
    document.getElementById('code').focus();
  } catch (error) { message.textContent = error instanceof TypeError ? 'Cannot connect to the server.' : error.message; }
  finally { requesting = false; document.getElementById('request-code').disabled = false; }
});
resetForm.addEventListener('submit', async (event) => {
  event.preventDefault();
  const button = document.getElementById('reset-password');
  if (button.disabled) return;
  button.disabled = true;
  try {
    await post('/api/password/reset', {...Object.fromEntries(new FormData(resetForm)), challenge});
    resetForm.reset();
    resetForm.hidden = true;
    requestForm.hidden = true;
    message.textContent = 'Password changed. Go back to sign in and use your new password.';
    challenge = '';
  } catch (error) { message.textContent = error instanceof TypeError ? 'Cannot connect to the server.' : error.message; }
  finally { button.disabled = false; }
});

