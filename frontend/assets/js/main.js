/**
 * Assemblage : saisie, validation, envoi, suivi.
 *
 * Règle qui gouverne ce fichier : le PAN et le CVV vivent dans leur champ et
 * dans le corps de la requête, nulle part ailleurs. Pas de `console.log`, pas
 * de stockage, pas de variable qui leur survive.
 */
(() => {
  'use strict';

  const { $ } = UI;

  document.addEventListener('DOMContentLoaded', () => {
    const form = $('payment-form');

    // ── Panneau de connexion ────────────────────────────────────────────
    const saved = Gateway.config.load();
    $('base-url').value = saved.baseUrl || '';
    $('api-key').value = saved.apiKey || '';

    const persist = () => Gateway.config.save({
      baseUrl: $('base-url').value.trim(),
      apiKey: $('api-key').value,
    });
    $('base-url').addEventListener('change', persist);
    $('api-key').addEventListener('change', persist);

    $('settings-toggle').addEventListener('click', () => {
      const panel = $('settings');
      panel.hidden = !panel.hidden;
      $('settings-toggle').setAttribute('aria-expanded', String(!panel.hidden));
    });

    async function checkHealth() {
      UI.showHealth('idle', 'vérification…');
      try {
        const data = await Gateway.health();
        UI.showHealth('ok', `en ligne · ${data.mode || 'inconnu'}`);
        return true;
      } catch (err) {
        UI.showHealth('err', 'injoignable');
        return false;
      }
    }
    $('health-check').addEventListener('click', checkHealth);

    /**
     * Détecte un relais devant nous.
     *
     * Aucune URL saisie et /health répond quand même : l'API est servie par
     * notre propre origine. Ni CORS ni clé d'API ne concernent alors la page.
     * La détection tient dans la requête de santé déjà faite au démarrage.
     */
    async function boot() {
      const reachable = await checkHealth();
      if (reachable && !Gateway.config.load().baseUrl) UI.setRelayed();
    }

    // ── Mise en forme pendant la frappe ─────────────────────────────────
    const pan = $('pan');
    pan.addEventListener('input', () => {
      // Le curseur repart en fin de champ : acceptable pour une saisie
      // linéaire, et cela évite une gestion d'offset fragile.
      pan.value = Card.formatPan(pan.value);
      const b = Card.brand(pan.value);
      $('brand').textContent = b.name;
      $('cvv').maxLength = b.cvv;
      UI.setFieldError('pan', 'pan-error', '');
    });
    pan.addEventListener('blur', () => {
      if (pan.value) UI.setFieldError('pan', 'pan-error', Card.panError(pan.value));
    });

    const expiry = $('expiry');
    expiry.addEventListener('input', () => {
      expiry.value = Card.formatExpiry(expiry.value);
      UI.setFieldError('expiry', 'expiry-error', '');
    });
    expiry.addEventListener('blur', () => {
      if (expiry.value) UI.setFieldError('expiry', 'expiry-error', Card.expiryError(expiry.value));
    });

    const cvv = $('cvv');
    cvv.addEventListener('input', () => {
      cvv.value = Card.digits(cvv.value).slice(0, 4);
      UI.setFieldError('cvv', 'cvv-error', '');
    });
    cvv.addEventListener('blur', () => {
      if (cvv.value) UI.setFieldError('cvv', 'cvv-error', Card.cvvError(cvv.value, pan.value));
    });

    ['amount', 'target-wallet'].forEach((id) => {
      const errorId = id === 'amount' ? 'amount-error' : 'wallet-error';
      $(id).addEventListener('input', () => UI.setFieldError(id, errorId, ''));
    });

    /** Valide tout le formulaire et peint les erreurs. */
    function validate() {
      const checks = [
        ['pan', 'pan-error', Card.panError(pan.value)],
        ['expiry', 'expiry-error', Card.expiryError(expiry.value)],
        ['cvv', 'cvv-error', Card.cvvError(cvv.value, pan.value)],
        ['amount', 'amount-error', Card.amountError($('amount').value)],
        ['target-wallet', 'wallet-error', Card.walletError($('target-wallet').value)],
      ];
      checks.forEach(([inputId, errorId, message]) => UI.setFieldError(inputId, errorId, message));
      const firstBad = checks.find(([, , message]) => message);
      if (firstBad) $(firstBad[0]).focus();
      return !firstBad;
    }

    /** Vide les champs sensibles dès que la requête est partie. */
    function clearCardFields() {
      pan.value = '';
      cvv.value = '';
      expiry.value = '';
      $('brand').textContent = '';
    }

    // ── Envoi ───────────────────────────────────────────────────────────
    // Incrémenté à chaque envoi. Le suivi d'un paiement précédent, encore en
    // cours, ne doit pas repeindre le panneau par-dessus le paiement courant.
    let generation = 0;

    form.addEventListener('submit', async (event) => {
      event.preventDefault();
      if (!validate()) return;

      const payload = {
        pan: Card.digits(pan.value),
        expiry: Card.expiryToApi(expiry.value),
        cvv: Card.digits(cvv.value),
        amount: $('amount').value.trim(),
        currency: $('currency').value,
        target_wallet: $('target-wallet').value.trim(),
      };
      // Ce que l'on affichera : jamais le PAN complet.
      const masked = Card.maskPan(payload.pan);

      UI.setBusy(true);
      UI.showStatus('pending', 'Envoi en cours', "Autorisation auprès de l'acquéreur…");
      UI.showDetails(null);

      const mine = (generation += 1);
      const current = () => generation === mine;

      let accepted;
      try {
        accepted = await Gateway.pay(payload);
      } catch (err) {
        const view = UI.describeHttpError(err.status);
        UI.showStatus(view.tone, view.label, err.message);
        UI.showDetails({ masked_pan: masked });
        UI.setBusy(false);
        return;
      } finally {
        // Le PAN a quitté la page : il n'a plus rien à faire ni dans le DOM ni
        // dans cette portée, que la passerelle ait répondu oui, non ou rien.
        clearCardFields();
        payload.pan = '';
        payload.cvv = '';
      }

      const first = UI.describe(accepted.status);
      // Le message serveur est en anglais : on lui préfère la traduction, et on
      // ne s'en sert que pour un état que cette page ne connaît pas encore.
      UI.showStatus(first.tone, first.label, first.message || accepted.message);
      UI.showDetails({ ...accepted, masked_pan: masked, currency: payload.currency });
      UI.setBusy(false);

      // ── Suivi de la remise crypto ─────────────────────────────────────
      const id = accepted.transaction_id;
      if (id === undefined || Gateway.TERMINAL.has(accepted.status)) return;

      await Gateway.poll(id, {
        onUpdate: (tx, err) => {
          if (err || !tx || !current()) return;
          const view = UI.describe(tx.status);
          UI.showStatus(view.tone, view.label, view.message);
          UI.showDetails({ ...tx, masked_pan: tx.masked_pan || masked });
        },
      });
    });

    boot();
  });
})();
