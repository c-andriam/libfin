/**
 * La console des liens de paiement.
 *
 * Trois actions et un inventaire. C'est la seule page qui montre les adresses
 * de destination, et la seule qui puisse supprimer : elle exige donc que la
 * clé d'API soit saisie dans « Connexion ». Le relais ne la fournit pas à sa
 * place — il le fait pour le paiement, jamais pour la gestion.
 */
(() => {
  'use strict';

  const { $ } = UI;

  document.addEventListener('DOMContentLoaded', () => {
    Boot.start();

    const rows = $('rows');

    /** L'adresse à envoyer au payeur, pour un jeton donné. */
    function paymentUrl(token) {
      const url = new URL('payment.html', window.location.href);
      url.search = new URLSearchParams({ l: token }).toString();
      return url.toString();
    }

    const el = (tag, className, text) => {
      const node = document.createElement(tag);
      if (className) node.className = className;
      if (text !== undefined) node.textContent = text;
      return node;
    };

    function row(link) {
      const panel = el('section', 'panel panel--result');
      panel.dataset.id = String(link.id);

      const head = el('div', 'recap__head');
      head.appendChild(el('span', 'recap__title', `${link.amount} ${link.currency}`));
      head.appendChild(el(
        'span',
        link.active ? 'pill pill--ok' : 'pill pill--idle',
        link.active ? 'actif' : 'désactivé',
      ));
      panel.appendChild(head);

      const recap = el('div', 'recap');
      const line = (label, value, mono) => {
        const r = el('div', 'recap__row');
        r.appendChild(el('span', 'recap__label', label));
        r.appendChild(el('span', `recap__value${mono ? ' recap__value--mono' : ''}`, value));
        recap.appendChild(r);
      };
      line('Destinataire', link.target_wallet, true);
      line('Créé le', new Date(link.created_at).toLocaleString());
      // Un compteur qui monte plus vite que prévu est le signe qu'un lien a
      // circulé : c'est la raison d'être de cette ligne.
      line('Paiements', String(link.payment_count));
      if (link.expires_at) line('Expire le', new Date(link.expires_at).toLocaleString());
      panel.appendChild(recap);

      const field = el('div', 'field');
      const input = el('input');
      input.type = 'text';
      input.readOnly = true;
      input.value = paymentUrl(link.token);
      field.appendChild(input);
      panel.appendChild(field);

      const actions = el('div', 'result__actions');

      const copy = el('button', 'btn btn--primary');
      copy.type = 'button';
      copy.appendChild(el('span', 'btn__label', 'Copier'));
      copy.addEventListener('click', async () => {
        try {
          if (!navigator.clipboard) throw new Error('indisponible');
          await navigator.clipboard.writeText(input.value);
          copy.querySelector('.btn__label').textContent = 'Copié';
          setTimeout(() => { copy.querySelector('.btn__label').textContent = 'Copier'; }, 1500);
        } catch (_) {
          input.focus();
          input.select();
        }
      });
      actions.appendChild(copy);

      const toggle = el('button', 'btn btn--ghost', link.active ? 'Désactiver' : 'Activer');
      toggle.type = 'button';
      toggle.addEventListener('click', async () => {
        toggle.disabled = true;
        try {
          await Gateway.setLinkActive(link.id, !link.active);
          await load();
        } catch (err) {
          fail(err);
          toggle.disabled = false;
        }
      });
      actions.appendChild(toggle);

      const remove = el('button', 'btn btn--ghost', 'Supprimer');
      remove.type = 'button';
      remove.addEventListener('click', async () => {
        // Une suppression est définitive et ce bouton est à côté des autres :
        // la confirmation rappelle ce qui disparaît et ce qui reste.
        const sure = window.confirm(
          `Supprimer définitivement le lien de ${link.amount} ${link.currency} ?\n\n`
          + 'Il cessera immédiatement de fonctionner. Les paiements déjà '
          + 'effectués restent enregistrés.',
        );
        if (!sure) return;
        remove.disabled = true;
        try {
          await Gateway.deleteLink(link.id);
          await load();
        } catch (err) {
          fail(err);
          remove.disabled = false;
        }
      });
      actions.appendChild(remove);

      panel.appendChild(actions);
      return panel;
    }

    function fail(err) {
      if (err.status === 401) {
        UI.showBanner(
          'Clé d’API requise : ouvrez « Connexion » et saisissez-la. Cette page '
          + 'ne bénéficie pas de la clé du relais, volontairement.',
        );
        return;
      }
      const view = UI.describeHttpError(err.status);
      UI.showBanner(`${view.label} — ${err.message}`, 'error');
    }

    async function load() {
      UI.hideBanner();
      let list;
      try {
        list = await Gateway.links();
      } catch (err) {
        fail(err);
        return;
      }
      rows.textContent = '';
      $('empty').hidden = list.length > 0;
      list.forEach((link) => rows.appendChild(row(link)));
    }

    $('reload').addEventListener('click', load);
    load();
  });
})();
