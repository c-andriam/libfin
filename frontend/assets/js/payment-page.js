/**
 * Étape 2 — la carte.
 *
 * Règle qui gouverne ce fichier : le PAN et le CVV vivent dans leur champ et
 * dans le corps de la requête, nulle part ailleurs. Pas de `console.log`, pas
 * de stockage, pas de variable qui leur survive.
 *
 * La commande arrive de `sessionStorage` ou de l'URL. Dans les deux cas elle
 * est revalidée : un paramètre d'URL est une saisie comme une autre, et un
 * montant trafiqué ne doit pas atteindre la passerelle par cette page.
 */
(() => {
  'use strict';

  const { $ } = UI;

  document.addEventListener('DOMContentLoaded', () => {
    // ── Récupération et contrôle de la commande ─────────────────────────────
    // L'URL prime : c'est elle qui décrit la page qu'on a demandée. Le
    // stockage ne sert que de repli quand l'URL ne porte rien.
    const order = Order.fromQuery() || Order.load();

    if (!order) {
      UI.block("Aucune commande n'accompagne cette page. Reprenez depuis le montant.");
      return;
    }

    const { errors, warnings } = Order.validate(order);
    const problems = Object.values(errors);
    if (problems.length) {
      UI.block(`La commande reçue est invalide : ${problems.join(' ')}`);
      return;
    }

    // Commande valide : on peut montrer les champs carte.
    $('checkout').hidden = false;
    $('recap-amount').textContent = Order.format(order);
    const walletCell = $('recap-wallet');
    walletCell.textContent = Address.shorten(order.wallet);
    walletCell.title = order.wallet;

    UI.setBusy(false, `Payer ${Order.format(order)}`);
    if (warnings.wallet) UI.showBanner(warnings.wallet, 'warn');

    // La commande venue de l'URL devient la référence pour l'étape 3.
    Order.save({ ...order, createdAt: order.createdAt || new Date().toISOString() });

    Boot.start().then((reachable) => {
      if (!reachable) {
        UI.showBanner(
          'La passerelle est injoignable : le paiement échouerait. '
          + 'Vérifiez la connexion avant de saisir votre carte.',
        );
      }
    });

    // ── Mise en forme pendant la frappe ─────────────────────────────────────
    const pan = $('pan');
    pan.addEventListener('input', () => {
      // Le curseur repart en fin de champ : acceptable pour une saisie
      // linéaire, et cela évite une gestion d'offset fragile.
      pan.value = Card.formatPan(pan.value);
      const brand = Card.brand(pan.value);
      $('brand').textContent = brand.name;
      $('cvv').maxLength = brand.cvv;
      UI.setFieldError('pan', 'pan-error', '');
      UI.hideBanner();
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

    /** Valide les trois champs carte et peint les erreurs. */
    function validate() {
      const checks = [
        ['pan', 'pan-error', Card.panError(pan.value)],
        ['expiry', 'expiry-error', Card.expiryError(expiry.value)],
        ['cvv', 'cvv-error', Card.cvvError(cvv.value, pan.value)],
      ];
      checks.forEach(([id, errorId, message]) => UI.setFieldError(id, errorId, message));
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

    // ── Envoi ───────────────────────────────────────────────────────────────
    let sending = false;

    $('payment-form').addEventListener('submit', async (event) => {
      event.preventDefault();
      // Un second envoi pendant que le premier attend l'acquéreur créerait une
      // deuxième transaction : le bouton est désactivé, ce drapeau couvre le
      // reste (touche Entrée, script, double événement).
      if (sending) return;
      if (!validate()) return;

      UI.hideBanner();

      const payload = {
        pan: Card.digits(pan.value),
        expiry: Card.expiryToApi(expiry.value),
        cvv: Card.digits(cvv.value),
        amount: order.amount,
        currency: order.currency,
        target_wallet: order.wallet,
      };

      sending = true;
      UI.setBusy(true, 'Traitement…');

      let accepted;
      try {
        accepted = await Gateway.pay(payload);
      } catch (err) {
        const view = UI.describeHttpError(err.status);
        UI.showBanner(`${view.label} — ${err.message}`, view.tone === 'declined' ? 'warn' : 'error');
        sending = false;
        UI.setBusy(false, `Payer ${Order.format(order)}`);
        return;
      } finally {
        // Le PAN a quitté la page : il n'a plus rien à faire ni dans le DOM ni
        // dans cette portée, que la passerelle ait répondu oui, non ou rien.
        clearCardFields();
        payload.pan = '';
        payload.cvv = '';
      }

      const id = accepted.transaction_id;

      // Mode PayMeGate : la passerelle a renvoyé un checkout d'hébergement.
      // Le client paie sur la page du prestataire ; on y redirige dès que
      // possible. L'état de la transaction reste suivi via result.html?tx=…
      // quand il revient, car le webhook order.paid l'a marquée payée.
      if (accepted.checkout_url) {
        window.location.assign(accepted.checkout_url);
        return;
      }

      if (id === undefined || id === null) {
        // Ne devrait pas arriver : PaymentResponse le déclare obligatoire. Si
        // cela arrive tout de même, mieux vaut le dire que rediriger à vide.
        UI.showBanner(
          "La passerelle a accepté le paiement sans renvoyer d'identifiant. "
          + 'Contactez le support avant de réessayer : un débit a pu avoir lieu.',
        );
        sending = false;
        UI.setBusy(false, `Payer ${Order.format(order)}`);
        return;
      }

      try {
        window.location.assign(`result.html?tx=${encodeURIComponent(id)}`);
      } catch (err) {
        // Le paiement est parti : surtout ne pas laisser croire le contraire.
        UI.showBanner(
          `Paiement accepté (transaction ${id}), mais la redirection a échoué. `
          + `Ouvrez result.html?tx=${id} pour suivre son état. Ne recommencez pas le paiement.`,
          'warn',
        );
        sending = false;
        UI.setBusy(false, 'Paiement envoyé');
      }
    });
  });
})();
