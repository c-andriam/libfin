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
    // Un jeton de lien de paiement, s'il y en a un. C'est désormais la forme
    // normale : le lien ne porte que cela, et la commande vit sur le serveur.
    const token = new URLSearchParams(window.location.search).get('l');
    // L'ancienne forme — commande en clair dans l'URL — reste lue pour ne pas
    // casser un lien déjà distribué, mais elle n'est plus produite.
    const fromLink = token ? { token } : Order.fromQuery();
    let order = token ? null : (Order.fromQuery() || Order.load());

    // Une commande arrivée par l'URL est un lien de paiement : la personne
    // devant l'écran n'est pas celle qui l'a fabriqué. Elle n'a donc que faire
    // du fil d'étapes, qui décrit un parcours qu'elle n'a pas suivi, ni de la
    // marque et de l'état de la passerelle, qui renseignent l'exploitant.
    // Retirer « Modifier » n'est pas cosmétique : ce lien renvoie le payeur
    // vers un formulaire où il peut réécrire le montant et le destinataire.
    if (fromLink) stripMerchantChrome();

    function stripMerchantChrome() {
      ['topbar', 'steps', 'recap-edit'].forEach((id) => {
        const node = document.getElementById(id);
        if (node) node.remove();
      });
      // Le repli d'erreur invite à « revenir à l'étape 1 », ce qui n'a pas de
      // sens pour un payeur : la commande n'est pas la sienne à refaire.
      const action = document.getElementById('blocked-action');
      if (action) action.remove();
      document.body.classList.add('is-hosted-link');
    }

    // Aucun `return` dans cette section. Tout ce qui suit dans ce gestionnaire
    // enregistre les écouteurs du formulaire, dont celui du bouton de paiement ;
    // sortir ici les laisserait non branchés et le bouton resterait inerte. Ce
    // qui décide si l'on peut payer, c'est la visibilité de #checkout, révélée
    // par begin() — pas l'exécution de cette fonction.
    if (token) {
      // Le serveur dit ce que ce lien facture. Le montant est la seule chose
      // que le payeur apprend, et la seule qu'il ait besoin de savoir : on ne
      // demande pas à quelqu'un d'autoriser un débit dont on lui cache la
      // somme. L'adresse de destination ne descend jamais jusqu'ici.
      openLink(token);
    } else if (!order) {
      UI.block("Aucune commande n'accompagne cette page. Reprenez depuis le montant.");
    } else {
      begin(order);
    }

    async function openLink(value) {
      let view;
      try {
        view = await Gateway.readLink(value);
      } catch (err) {
        // 404 couvre indifféremment inconnu, expiré et déjà payé : le serveur
        // ne distingue pas, pour ne rien apprendre à qui essaie des jetons.
        UI.block(err.status === 404
          ? 'Ce lien de paiement n’est plus valable — il a expiré, a déjà servi, '
            + 'ou n’a jamais existé. Demandez-en un nouveau à son expéditeur.'
          : `Ce lien n’a pas pu être vérifié : ${err.message}`);
        return;
      }
      order = {
        amount: String(view.amount),
        currency: Order.currencyLabel(view.currency),
        // Volontairement absente. Rien sur cette page n'en a besoin : le
        // paiement se fait au nom du jeton, et le serveur sait où livrer.
        wallet: null,
      };
      begin(order);
    }

    function begin(current) {
      // Sans jeton, la commande est une saisie comme une autre et se valide
      // comme telle. Avec un jeton, elle vient du serveur : la revalider ici
      // reviendrait à faire confiance à la page pour juger sa propre source.
      if (!token) {
        const { errors, warnings } = Order.validate(current);
        const problems = Object.values(errors);
        if (problems.length) {
          UI.block(`La commande reçue est invalide : ${problems.join(' ')}`);
          return false;
        }
        if (warnings.wallet) UI.showBanner(warnings.wallet, 'warn');
        Order.save({ ...current, createdAt: current.createdAt || new Date().toISOString() });
      }

      $('checkout').hidden = false;
      UI.setBusy(false, `Payer ${Order.format(current)}`);
      return true;
    }

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
        // Avec un jeton, on n'envoie ni montant ni destinataire : les envoyer
        // serait offrir la prise que ce mécanisme existe pour retirer. L'API
        // refuse d'ailleurs une requête qui porte les deux.
        ...(token
          ? { link: token }
          : { amount: order.amount, currency: order.currency, target_wallet: order.wallet }),
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
        // Le marqueur voyage dans l'URL, pas dans le stockage : la page de
        // confirmation se recharge, se met en favori et se partage, et un
        // payeur ne doit pas se retrouver devant la configuration du marchand
        // parce que son navigateur a vidé une clé de session.
        const next = token
          ? `result.html?tx=${encodeURIComponent(id)}&l=1`
          : `result.html?tx=${encodeURIComponent(id)}`;
        window.location.assign(next);
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
