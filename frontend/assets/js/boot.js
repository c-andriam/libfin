/**
 * Amorçage commun aux trois pages : panneau de connexion, état de la
 * passerelle, détection du relais.
 */
const Boot = (() => {
  'use strict';

  const { $ } = UI;

  /**
   * Lit un jeton `make connect` dans l'URL (`#connect=...`), l'installe comme
   * clé d'API et nettoie l'URL affichée.
   *
   * Ce jeton n'est jamais la clé permanente : il vient d'une session
   * temporaire créée côté serveur, propre à la console des liens, et expire
   * de lui-même. Le stocker comme une clé d'API ordinaire suffit — la
   * passerelle fait la différence, pas le navigateur.
   */
  function consumeConnectToken() {
    const match = /(?:^|[#&])connect=([^&]+)/.exec(window.location.hash);
    if (!match) return false;
    Gateway.config.save({ ...Gateway.config.load(), apiKey: decodeURIComponent(match[1]) });
    history.replaceState(null, '', window.location.pathname + window.location.search);
    return true;
  }

  /** Interroge /health et peint la pastille. Renvoie l'accessibilité. */
  async function checkHealth() {
    UI.showHealth('idle', 'vérification…');
    try {
      const data = await Gateway.health();
      UI.showHealth('ok', `en ligne · ${data.mode || 'inconnu'}`);
      return true;
    } catch (_) {
      UI.showHealth('err', 'injoignable');
      return false;
    }
  }

  /** Câble le panneau de connexion, sur les pages qui en portent un. */
  function wireSettings() {
    const toggle = $('settings-toggle');
    if (!toggle) return;

    const saved = Gateway.config.load();
    $('base-url').value = saved.baseUrl || '';
    $('api-key').value = saved.apiKey || '';

    const persist = () => Gateway.config.save({
      baseUrl: $('base-url').value.trim(),
      apiKey: $('api-key').value,
    });
    $('base-url').addEventListener('change', persist);
    $('api-key').addEventListener('change', persist);

    toggle.addEventListener('click', () => {
      const panel = $('settings');
      panel.hidden = !panel.hidden;
      toggle.setAttribute('aria-expanded', String(!panel.hidden));
    });

    if ($('health-check')) $('health-check').addEventListener('click', checkHealth);
  }

  /**
   * Démarre la page.
   *
   * Aucune URL saisie et /health répond quand même : l'API est servie par notre
   * propre origine, donc par un relais. Ni CORS ni clé d'API ne concernent
   * alors la page, et le panneau de connexion n'a plus d'objet — sauf si un
   * jeton `make connect` vient d'être installé : la console des liens n'hérite
   * jamais de la clé du relais (voir nginx.conf), donc ce jeton est ce qui la
   * rend accessible, et masquer le panneau l'effacerait aussitôt.
   */
  async function start() {
    const connected = consumeConnectToken();
    wireSettings();
    const reachable = await checkHealth();
    if (reachable && connected) {
      UI.showBanner('Session temporaire active pour cette console.', 'warn');
    } else if (reachable && !Gateway.config.load().baseUrl) {
      UI.setRelayed();
    }
    return reachable;
  }

  return { start, checkHealth };
})();
