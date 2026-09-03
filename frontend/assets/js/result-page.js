/**
 * Étape 3 — l'état de la transaction.
 *
 * Adressée par l'identifiant, `result.html?tx=42`, et non par un état gardé en
 * mémoire : la page se recharge, se met en favori et se partage sans rien
 * perdre. Tout ce qu'elle affiche vient de `GET /transaction/{id}`.
 */
(() => {
  'use strict';

  const { $ } = UI;

  document.addEventListener('DOMContentLoaded', () => {
    const params = new URLSearchParams(window.location.search);
    const raw = params.get('tx');

    // Arrivé par un lien de paiement : la personne devant l'écran est le
    // payeur, pas le marchand. Tout ce qui mène à la configuration du 2D link
    // doit disparaître — « Nouveau paiement » y renvoie directement, et le fil
    // d'étapes y renvoie par son étape 1. Le payeur n'a rien à y faire : il y
    // verrait comment les liens sont fabriqués, et pourrait s'en fabriquer.
    if (params.get('l') === '1') stripMerchantChrome();

    function stripMerchantChrome() {
      ['topbar', 'steps', 'blocked-new', 'result-new'].forEach((id) => {
        const node = document.getElementById(id);
        if (node) node.remove();
      });
      document.body.classList.add('is-hosted-link');
    }

    if (!raw) {
      UI.block("Cette page attend un identifiant de transaction, par exemple « result.html?tx=42 ».");
      return;
    }
    if (!/^\d+$/.test(raw)) {
      UI.block(`« ${raw} » n'est pas un identifiant de transaction : seuls des chiffres sont attendus.`);
      return;
    }

    const id = raw;
    $('result').hidden = false;
    $('permalink-note').textContent =
      'Cette page peut être rechargée ou mise en favori : son adresse contient '
      + `l'identifiant de la transaction (${id}).`;

    UI.showStatus('pending', 'Chargement…', "Interrogation de la passerelle.");

    let polling = false;

    /** Peint un état renvoyé par la passerelle. */
    function render(tx) {
      const view = UI.describe(tx.status);
      UI.showStatus(view.tone, view.label, view.message);
      UI.showDetails(tx);
    }

    /** Traduit un échec de lecture, sans jamais laisser la page muette. */
    function renderError(err) {
      const view = UI.describeHttpError(err.status);
      if (err.status === 404) {
        UI.block(
          `Aucune transaction ${id} chez la passerelle. `
          + "L'identifiant est peut-être erroné, ou la transaction appartient à une autre passerelle.",
        );
        $('result').hidden = true;
        return;
      }
      UI.showStatus(view.tone, view.label, err.message);
    }

    /** Une lecture, puis le suivi si l'état n'est pas encore terminal. */
    async function refresh() {
      if (polling) return;
      polling = true;
      $('refresh').disabled = true;

      let tx;
      try {
        tx = await Gateway.transaction(id);
      } catch (err) {
        renderError(err);
        polling = false;
        $('refresh').disabled = false;
        return;
      }

      render(tx);

      if (!Gateway.TERMINAL.has(tx.status)) {
        // La remise crypto dépend d'un bloc, pas de nous : on interroge jusqu'à
        // un état terminal, puis on s'arrête.
        const last = await Gateway.poll(id, {
          onUpdate: (update, err) => {
            if (err) return renderError(err);
            if (update) render(update);
          },
        });
        if (last && !Gateway.TERMINAL.has(last.status)) {
          UI.showBanner(
            "Le suivi automatique s'est arrêté avant l'issue de la transaction. "
            + '« Actualiser » relance l’interrogation.',
            'warn',
          );
        }
      }

      polling = false;
      $('refresh').disabled = false;
    }

    $('refresh').addEventListener('click', () => {
      UI.hideBanner();
      refresh();
    });

    // La commande a abouti : la garder ferait repartir l'étape 1 sur d'anciennes
    // valeurs. L'adresse de cette page suffit désormais à tout retrouver.
    Order.clear();

    Boot.start().then(refresh);
  });
})();
