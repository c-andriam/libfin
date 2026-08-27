/**
 * Étape 1 — la commande.
 *
 * Valide le montant et l'adresse, puis passe la main à l'étape 2. Rien n'est
 * envoyé à la passerelle ici : la page se contente de vérifier qu'elle est
 * joignable, pour ne pas envoyer le client saisir sa carte pour rien.
 */
(() => {
  'use strict';

  const { $ } = UI;

  document.addEventListener('DOMContentLoaded', () => {
    const form = $('order-form');
    let gatewayReachable = true;

    // Reprend une commande déjà saisie : revenir de l'étape 2 ne doit rien perdre.
    const existing = Order.load() || Order.fromQuery();
    if (existing) {
      $('amount').value = existing.amount || '';
      $('wallet').value = existing.wallet || '';
      if (Order.CURRENCIES.includes(existing.currency)) $('currency').value = existing.currency;
    }

    Boot.start().then((reachable) => {
      gatewayReachable = reachable;
      if (!reachable) {
        UI.showBanner(
          "La passerelle est injoignable. Vérifiez l'URL dans « Connexion » : "
          + 'inutile de saisir une carte tant que le paiement ne peut pas aboutir.',
          'warn',
        );
      }
    });

    // ── Saisie ──────────────────────────────────────────────────────────────
    $('amount').addEventListener('input', () => {
      UI.setFieldError('amount', 'amount-error', '');
      UI.hideBanner();
    });

    const wallet = $('wallet');
    wallet.addEventListener('input', () => {
      UI.setFieldError('wallet', 'wallet-error', '');
      UI.setFieldWarning('wallet-warn', '');
      UI.hideBanner();
    });
    wallet.addEventListener('blur', () => {
      if (!wallet.value.trim()) return;
      const seen = Address.inspect(wallet.value);
      UI.setFieldError('wallet', 'wallet-error', seen.error || '');
      UI.setFieldWarning('wallet-warn', seen.error ? '' : (seen.warning || ''));
      // Une adresse valide est réécrite dans sa casse EIP-55 : le client voit
      // la forme canonique, et la compare plus facilement à sa source.
      if (!seen.error && seen.checksummed) wallet.value = seen.checksummed;
    });

    // ── Validation et passage à l'étape 2 ───────────────────────────────────
    form.addEventListener('submit', (event) => {
      event.preventDefault();
      UI.hideBanner();

      const order = {
        amount: $('amount').value.trim(),
        currency: $('currency').value,
        wallet: $('wallet').value.trim(),
      };

      const { errors, warnings } = Order.validate(order);
      UI.setFieldError('amount', 'amount-error', errors.amount || '');
      UI.setFieldError('wallet', 'wallet-error', errors.wallet || '');
      UI.setFieldWarning('wallet-warn', errors.wallet ? '' : (warnings.wallet || ''));

      if (errors.currency) UI.showBanner(errors.currency);

      const firstBad = ['amount', 'wallet'].find((f) => errors[f]);
      if (firstBad || errors.currency) {
        if (firstBad) $(firstBad).focus();
        return;
      }

      if (!gatewayReachable) {
        UI.showBanner(
          'La passerelle est toujours injoignable. Corrigez la connexion avant de continuer.',
        );
        return;
      }

      // Le stockage peut être refusé (navigation privée). Ce n'est pas
      // bloquant : la commande voyage aussi dans l'URL de l'étape 2.
      const stored = Order.save({ ...order, createdAt: new Date().toISOString() });
      const target = `payment.html?${Order.toQuery(order)}`;

      try {
        window.location.assign(target);
      } catch (err) {
        UI.showBanner(
          `La redirection a échoué (${err && err.message ? err.message : 'raison inconnue'}). `
          + 'Ouvrez la page de paiement manuellement.',
        );
        return;
      }

      if (!stored) {
        // Rien à faire de plus : l'URL porte la commande. On le dit quand même,
        // au cas où la navigation tarderait.
        UI.showBanner('Stockage du navigateur indisponible ; la commande voyage par l’URL.', 'warn');
      }
    });
  });
})();
