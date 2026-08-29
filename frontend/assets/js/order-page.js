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

      // Le stockage garde la commande si l'opérateur revient la modifier. Le
      // lien, lui, n'en dépend plus : il ne porte qu'un jeton.
      Order.save({ ...order, createdAt: new Date().toISOString() });
      issueLink(order);
    });

    async function issueLink(order) {
      UI.setBusy(true, 'Création du lien…');
      let issued;
      try {
        issued = await Gateway.createLink(order);
      } catch (err) {
        const view = UI.describeHttpError(err.status);
        UI.showBanner(`${view.label} — ${err.message}`, 'error');
        return;
      } finally {
        UI.setBusy(false, 'Générer le lien de paiement');
      }
      showLink(order, issued);
    }

    // ── Le lien ─────────────────────────────────────────────────────────────
    // Cette page ne conduit plus le payeur à l'étape 2 : elle fabrique une
    // adresse qu'on lui transmet. Le marchand et le payeur sont rarement la
    // même personne, ni sur le même appareil.
    function paymentUrl(token) {
      // Résolue contre l'URL courante plutôt que construite à la main : la page
      // peut être servie depuis un sous-répertoire, et un chemin absolu codé en
      // dur produirait un lien mort dès qu'elle l'est.
      const url = new URL('payment.html', window.location.href);
      // Le jeton, et rien d'autre. Le montant et l'adresse sont restés côté
      // serveur : ils ne sont ni lisibles par qui reçoit le lien, ni
      // modifiables par qui le paie.
      url.search = new URLSearchParams({ l: token }).toString();
      return url.toString();
    }

    function showLink(order, issued) {
      const link = paymentUrl(issued.token);
      $('link-url').value = link;
      $('link-open').href = link;
      $('link-amount').textContent = Order.format(order);
      $('link-wallet').textContent = order.wallet;

      // L'expiration est décidée par le serveur, pas devinée ici : une durée
      // affichée qui ne serait pas celle appliquée tromperait le marchand sur
      // le temps dont dispose son client.
      const expires = issued && issued.expires_at ? new Date(issued.expires_at) : null;
      const dated = expires && !Number.isNaN(expires.valueOf());
      // Sans date, le lien vit jusqu'à ce qu'on le retire : le dire ainsi
      // plutôt qu'afficher une échéance vide, qui laisserait croire à un
      // réglage manquant.
      $('link-expires').textContent = dated ? expires.toLocaleString() : 'jusqu’à suppression';
      $('link-expiry').textContent = dated
        ? `Il expire le ${expires.toLocaleString()}.`
        : 'Il reste valable tant que vous ne le retirez pas.';

      $('link-panel').hidden = false;
      form.hidden = true;
      UI.hideBanner();
      setLinkStatus('');
      $('link-panel').scrollIntoView({ behavior: 'smooth', block: 'start' });
      $('link-copy').focus();
    }

    function setLinkStatus(message, tone) {
      const node = $('link-status');
      node.textContent = message || '';
      node.className = tone ? `status status--${tone}` : 'status';
      node.hidden = !message;
    }

    $('link-copy').addEventListener('click', async () => {
      const value = $('link-url').value;
      try {
        // navigator.clipboard n'existe qu'en contexte sécurisé, et échoue aussi
        // quand la permission est refusée. La sélection reste le repli qui
        // marche partout : le lien est prêt à copier au clavier.
        if (!navigator.clipboard) throw new Error('presse-papiers indisponible');
        await navigator.clipboard.writeText(value);
        setLinkStatus('Lien copié.', 'ok');
      } catch (_) {
        const field = $('link-url');
        field.focus();
        field.select();
        setLinkStatus('Copie automatique refusée par le navigateur — le lien est sélectionné, faites Ctrl+C.', 'pending');
      }
    });

    $('link-back').addEventListener('click', () => {
      $('link-panel').hidden = true;
      form.hidden = false;
      setLinkStatus('');
      $('amount').focus();
    });
  });
})();
