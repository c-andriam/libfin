(() => {
  'use strict';
  const $ = (id) => document.getElementById(id);
  const amount = $('amount'), currency = $('currency'), payBtn = $('pay');
  const amountErr = $('amount-err'), formErr = $('form-err'), okMsg = $('ok-msg');

  async function submit() {
    formErr.textContent = '';
    okMsg.classList.add('hidden');
    const value = (amount.value || '').trim();
    if (!/^\d+(\.\d{1,2})?$/.test(value) || parseFloat(value) <= 0) {
      amountErr.textContent = 'Montant invalide.';
      return;
    }
    amountErr.textContent = '';
    payBtn.disabled = true;
    payBtn.textContent = 'Création du paiement…';
    try {
      const resp = await fetch('/checkout/pay', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ amount: value, currency: currency.value }),
      });
      const data = await resp.json();
      if (!resp.ok || !data.checkout_url) {
        throw new Error(data.detail || data.message || 'Erreur(' + resp.status + ')');
      }
      window.location.assign(data.checkout_url);
    } catch (err) {
      formErr.textContent = err.message || 'Le paiement est indisponible, réessayez.';
      payBtn.disabled = false;
      payBtn.textContent = 'Payer';
    }
  }

  payBtn.addEventListener('click', submit);
  amount.addEventListener('keydown', (e) => { if (e.key === 'Enter') submit(); });
})();
