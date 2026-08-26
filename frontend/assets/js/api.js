/**
 * Client de la passerelle : le seul endroit qui parle réseau.
 *
 * Contrat côté serveur — src/gateway/api.py :
 *   POST /pay                → 202 { status, message, transaction_id, fiat_amount, tx_hash, stan }
 *   GET  /transaction/{id}   → 200 { id, status, masked_pan, stan, rrn, crypto_tx_hash, ... }
 *   GET  /health             → 200 { status, mode }   (seule route publique)
 */
const Gateway = (() => {
  'use strict';

  const STORAGE_KEY = 'libfin.gateway';

  /** États depuis lesquels la transaction ne bougera plus. */
  const TERMINAL = new Set([
    'FIAT_CAPTURED', 'AUTH_VOIDED', 'FIAT_DECLINED',
    'CRYPTO_SENT', 'CRYPTO_FAILED', 'REVERSED', 'REVERSAL_FAILED',
  ]);

  /**
   * Une erreur portant le code HTTP, pour que l'appelant distingue un refus
   * bancaire (422/402) d'une panne (503) sans analyser un texte.
   */
  class GatewayError extends Error {
    constructor(message, status, payload) {
      super(message);
      this.name = 'GatewayError';
      this.status = status;
      this.payload = payload;
    }
  }

  /**
   * Configuration de connexion.
   *
   * En `sessionStorage` et non `localStorage` : la clé disparaît à la
   * fermeture de l'onglet. Aucune donnée de carte n'y transite jamais.
   */
  const config = {
    load() {
      try {
        return JSON.parse(sessionStorage.getItem(STORAGE_KEY)) || {};
      } catch (_) {
        return {};
      }
    },
    save(values) {
      try {
        sessionStorage.setItem(STORAGE_KEY, JSON.stringify(values));
      } catch (_) {
        /* Navigation privée ou stockage bloqué : on tourne sans persistance. */
      }
    },
    baseUrl() {
      const stored = (this.load().baseUrl || '').trim();
      return (stored || window.location.origin).replace(/\/+$/, '');
    },
    apiKey() {
      return (this.load().apiKey || '').trim();
    },
  };

  /** Clé d'idempotence conforme à IDEMPOTENCY_KEY_PATTERN côté passerelle. */
  function idempotencyKey() {
    const uuid = (window.crypto && window.crypto.randomUUID)
      ? window.crypto.randomUUID()
      : Array.from({ length: 4 }, () => Math.random().toString(16).slice(2, 10)).join('-');
    return `ui-${uuid}`.slice(0, 64);
  }

  /** Extrait un message lisible, quelle que soit la forme de l'erreur. */
  function readError(payload, status) {
    if (!payload) return `Erreur ${status}.`;
    const { detail } = payload;
    if (typeof detail === 'string') return detail;
    if (Array.isArray(detail)) {
      // 422 de Pydantic : [{ loc: ["body", "pan"], msg: "..." }, ...]
      return detail
        .map((e) => {
          const field = Array.isArray(e.loc) ? e.loc[e.loc.length - 1] : '';
          return field ? `${field} : ${e.msg}` : e.msg;
        })
        .join(' · ');
    }
    if (typeof payload.error === 'string') return payload.error;  // slowapi, 429
    if (typeof payload.message === 'string') return payload.message;
    return `Erreur ${status}.`;
  }

  /** Appel HTTP unique : en-têtes, délai maximal, décodage des erreurs. */
  async function request(path, { method = 'GET', body = null, headers = {}, timeoutMs = 45000 } = {}) {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), timeoutMs);

    const finalHeaders = { ...headers };
    const key = config.apiKey();
    if (key) finalHeaders['X-API-Key'] = key;
    if (body !== null) finalHeaders['Content-Type'] = 'application/json';

    let response;
    try {
      response = await fetch(config.baseUrl() + path, {
        method,
        headers: finalHeaders,
        body: body === null ? undefined : JSON.stringify(body),
        signal: controller.signal,
        // Aucun cookie : la passerelle s'authentifie par en-tête.
        credentials: 'omit',
        cache: 'no-store',
        mode: 'cors',
      });
    } catch (err) {
      clearTimeout(timer);
      if (err.name === 'AbortError') {
        // Le sort du débit est inconnu : ne jamais suggérer de réessayer.
        throw new GatewayError(
          "La passerelle n'a pas répondu à temps. Vérifiez l'état de la transaction avant de recommencer.",
          0, null,
        );
      }
      throw new GatewayError(
        "Passerelle injoignable. Vérifiez l'URL, le certificat TLS et CORS_ORIGINS.",
        0, null,
      );
    }
    clearTimeout(timer);

    let payload = null;
    try {
      payload = await response.json();
    } catch (_) {
      /* Une réponse sans corps JSON reste exploitable via le code HTTP. */
    }

    if (!response.ok) throw new GatewayError(readError(payload, response.status), response.status, payload);
    return payload;
  }

  /** Liveness. Route publique : répond même sans clé d'API. */
  const health = () => request('/health', { timeoutMs: 8000 });

  /**
   * Envoie le paiement. Le 202 signifie « fiat autorisé » ; la remise crypto
   * est asynchrone et se suit avec `transaction()`.
   */
  const pay = (payload) => request('/pay', {
    method: 'POST',
    body: payload,
    headers: { 'Idempotency-Key': idempotencyKey() },
  });

  /** État courant d'une transaction. */
  const transaction = (id) => request(`/transaction/${encodeURIComponent(id)}`, { timeoutMs: 15000 });

  /**
   * Interroge `/transaction/{id}` jusqu'à un état terminal.
   *
   * Intervalle fixe et plafond de tentatives : la remise crypto dépend d'un
   * bloc, pas de nous, et une page laissée ouverte ne doit pas marteler la
   * passerelle indéfiniment.
   */
  async function poll(id, { onUpdate, intervalMs = 2500, attempts = 24 } = {}) {
    let last = null;
    for (let i = 0; i < attempts; i += 1) {
      await new Promise((resolve) => setTimeout(resolve, intervalMs));
      try {
        last = await transaction(id);
      } catch (err) {
        // Un incident réseau pendant le suivi ne change rien au paiement :
        // on cesse de suivre en gardant le dernier état connu.
        if (onUpdate) onUpdate(last, err);
        return last;
      }
      if (onUpdate) onUpdate(last, null);
      if (TERMINAL.has(last.status)) return last;
    }
    return last;
  }

  return { config, health, pay, transaction, poll, GatewayError, TERMINAL, idempotencyKey, readError };
})();

if (typeof module !== 'undefined' && module.exports) module.exports = Gateway;
