/**
 * Validation d'une adresse Ethereum, somme de contrôle comprise.
 *
 * Pourquoi tout ce code pour un champ texte : la passerelle n'accepte qu'une
 * expression régulière, `^0x[a-fA-F0-9]{40}$` (api.py). Une adresse dont un
 * caractère a été mal recopié la satisfait tout aussi bien — et le transfert
 * part alors vers une adresse qui n'appartient à personne, sans retour possible.
 *
 * EIP-55 encode une somme de contrôle dans la casse des lettres hexadécimales :
 * une adresse en casse mixte se vérifie, et attrape en pratique toutes les
 * fautes de frappe. Il faut Keccak-256 pour cela — d'où l'implémentation
 * ci-dessous, la seule primitive que ce projet ne pouvait pas éviter.
 *
 * Attention : Keccak-256 n'est pas SHA3-256. Ethereum utilise le Keccak
 * d'origine, dont le remplissage est 0x01 là où SHA-3 utilise 0x06.
 */
const Address = (() => {
  'use strict';

  const MASK = (1n << 64n) - 1n;

  const RC = [
    0x0000000000000001n, 0x0000000000008082n, 0x800000000000808an, 0x8000000080008000n,
    0x000000000000808bn, 0x0000000080000001n, 0x8000000080008081n, 0x8000000000008009n,
    0x000000000000008an, 0x0000000000000088n, 0x0000000080008009n, 0x000000008000000an,
    0x000000008000808bn, 0x800000000000008bn, 0x8000000000008089n, 0x8000000000008003n,
    0x8000000000008002n, 0x8000000000000080n, 0x000000000000800an, 0x800000008000000an,
    0x8000000080008081n, 0x8000000000008080n, 0x0000000080000001n, 0x8000000080008008n,
  ];

  //: Décalages de rotation, indexés par x + 5y.
  const ROT = [
    0n, 1n, 62n, 28n, 27n,
    36n, 44n, 6n, 55n, 20n,
    3n, 10n, 43n, 25n, 39n,
    41n, 45n, 15n, 21n, 8n,
    18n, 2n, 61n, 56n, 14n,
  ];

  const rotl = (x, n) => n === 0n ? x : ((x << n) | (x >> (64n - n))) & MASK;

  /** Permutation Keccak-f[1600], 24 tours sur 25 mots de 64 bits. */
  function keccakF(A) {
    const B = new Array(25);
    const C = new Array(5);
    const D = new Array(5);

    for (let round = 0; round < 24; round += 1) {
      // θ — diffuse la parité de chaque colonne sur les colonnes voisines.
      for (let x = 0; x < 5; x += 1) {
        C[x] = A[x] ^ A[x + 5] ^ A[x + 10] ^ A[x + 15] ^ A[x + 20];
      }
      for (let x = 0; x < 5; x += 1) {
        D[x] = C[(x + 4) % 5] ^ rotl(C[(x + 1) % 5], 1n);
      }
      for (let i = 0; i < 25; i += 1) {
        A[i] ^= D[i % 5];
      }

      // ρ et π — rotation de chaque mot, puis permutation des positions.
      for (let x = 0; x < 5; x += 1) {
        for (let y = 0; y < 5; y += 1) {
          B[y + 5 * ((2 * x + 3 * y) % 5)] = rotl(A[x + 5 * y], ROT[x + 5 * y]);
        }
      }

      // χ — la seule étape non linéaire.
      for (let y = 0; y < 5; y += 1) {
        for (let x = 0; x < 5; x += 1) {
          A[x + 5 * y] = B[x + 5 * y] ^ ((~B[(x + 1) % 5 + 5 * y] & MASK) & B[(x + 2) % 5 + 5 * y]);
        }
      }

      // ι — brise la symétrie entre les tours.
      A[0] ^= RC[round];
    }
    return A;
  }

  /**
   * Keccak-256 d'une suite d'octets, rendu en hexadécimal minuscule.
   *
   * Vérifié contre les vecteurs officiels pour l'entrée vide, « abc » et la
   * phrase du renard, ainsi que les huit adresses de l'EIP-55. L'absorption sur
   * plusieurs blocs (entrée de plus de 135 octets) n'est pas exercée ici : une
   * adresse fait toujours 40 octets.
   */
  function keccak256(bytes) {
    const RATE = 136;                       // 1088 bits pour une empreinte de 256
    const padded = new Uint8Array(Math.ceil((bytes.length + 1) / RATE) * RATE);
    padded.set(bytes);
    padded[bytes.length] = 0x01;            // remplissage Keccak, pas SHA-3
    padded[padded.length - 1] |= 0x80;

    const A = new Array(25).fill(0n);
    for (let offset = 0; offset < padded.length; offset += RATE) {
      for (let i = 0; i < RATE / 8; i += 1) {
        let lane = 0n;
        // Les mots sont absorbés en petit-boutiste.
        for (let b = 7; b >= 0; b -= 1) {
          lane = (lane << 8n) | BigInt(padded[offset + i * 8 + b]);
        }
        A[i] ^= lane;
      }
      keccakF(A);
    }

    let out = '';
    for (let i = 0; i < 4; i += 1) {
      let lane = A[i];
      for (let b = 0; b < 8; b += 1) {
        out += (lane & 0xffn).toString(16).padStart(2, '0');
        lane >>= 8n;
      }
    }
    return out;
  }

  const bytesOf = (text) => Uint8Array.from(text, (c) => c.charCodeAt(0));

  /**
   * Réécrit une adresse avec la casse EIP-55.
   *
   * Une lettre passe en majuscule quand le demi-octet correspondant de
   * l'empreinte de l'adresse minuscule vaut 8 ou plus.
   */
  function toChecksum(address) {
    const body = address.replace(/^0x/i, '').toLowerCase();
    const hash = keccak256(bytesOf(body));
    let out = '0x';
    for (let i = 0; i < body.length; i += 1) {
      out += parseInt(hash[i], 16) >= 8 ? body[i].toUpperCase() : body[i];
    }
    return out;
  }

  //: Adresses refusées d'emblée : un transfert vers elles est perdu à coup sûr.
  const BURN = new Set([
    '0x0000000000000000000000000000000000000000',
    '0x000000000000000000000000000000000000dead',
  ]);

  /**
   * Analyse une adresse saisie.
   *
   * Renvoie `{ error, warning, checksummed }`. `error` interdit de continuer ;
   * `warning` informe sans bloquer — le cas d'une adresse tout en minuscules,
   * parfaitement licite mais dont la somme de contrôle est indisponible.
   */
  function inspect(value) {
    const raw = (value || '').trim();
    if (!raw) return { error: 'Adresse du portefeuille requise.' };

    if (!raw.startsWith('0x') && !raw.startsWith('0X')) {
      return { error: 'Une adresse Ethereum commence par « 0x ».' };
    }
    const body = raw.slice(2);
    if (/[^0-9a-fA-F]/.test(body)) {
      const bad = body.match(/[^0-9a-fA-F]/)[0];
      return { error: `Caractère « ${bad} » interdit : seuls 0-9 et a-f sont admis.` };
    }
    if (body.length !== 40) {
      const diff = body.length - 40;
      return {
        error: diff < 0
          ? `Adresse trop courte de ${-diff} caractère${-diff > 1 ? 's' : ''} (40 attendus après « 0x »).`
          : `Adresse trop longue de ${diff} caractère${diff > 1 ? 's' : ''} (40 attendus après « 0x »).`,
      };
    }
    if (BURN.has(raw.toLowerCase())) {
      return { error: 'Cette adresse est une adresse de destruction : les fonds y seraient perdus.' };
    }

    const isLower = body === body.toLowerCase();
    const isUpper = body === body.toUpperCase();
    if (isLower || isUpper) {
      // Sans casse mixte, il n'y a aucune somme de contrôle à vérifier.
      return {
        warning: "Adresse sans somme de contrôle : vérifiez-la caractère par caractère, "
               + "un transfert crypto ne se rattrape pas.",
        checksummed: toChecksum(raw),
      };
    }

    const expected = toChecksum(raw);
    if (expected !== raw) {
      return {
        error: 'Somme de contrôle invalide (EIP-55) : cette adresse comporte une faute de frappe.',
      };
    }
    return { checksummed: expected };
  }

  /** Message d'erreur seul, pour les appels qui n'ont pas besoin du reste. */
  const error = (value) => inspect(value).error || '';

  /** `0x742d…f44e` — pour un récapitulatif, où la longueur nuit à la lecture. */
  function shorten(address, lead = 6, tail = 4) {
    const raw = (address || '').trim();
    if (raw.length <= lead + tail + 2) return raw;
    return `${raw.slice(0, 2 + lead)}…${raw.slice(-tail)}`;
  }

  return { keccak256, toChecksum, inspect, error, shorten, bytesOf };
})();

if (typeof module !== 'undefined' && module.exports) module.exports = Address;
