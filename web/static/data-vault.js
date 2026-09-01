(() => {
  'use strict';

  const FORMAT = 'rati-encrypted-data';
  const VERSION = 1;
  const ITERATIONS = 310000;
  const DB_NAME = 'rati-private-data';
  const STORE_NAME = 'vaults';
  const VAULT_ID = 'primary';
  const NOTICE_KEY = 'rati-data-vault-notice';
  const encoder = new TextEncoder();
  const decoder = new TextDecoder();
  const byId = id => document.getElementById(id);

  const dataHome = byId('dataHome');
  if (!dataHome) return;

  const status = byId('dataVaultStatus');
  const localChoice = byId('localStorageChoice');
  const localLabel = byId('localVaultLabel');
  const localHelp = byId('localVaultHelp');
  const localActions = byId('localVaultActions');
  const createDialog = byId('createVaultDialog');
  const openDialog = byId('openVaultDialog');
  const localDeleteDialog = byId('localDeleteDialog');
  let unlockedData = null;

  function setStatus(message, kind = '') {
    status.textContent = message;
    status.className = `data-vault-status ${kind}`.trim();
  }

  function bytesToBase64(bytes) {
    let binary = '';
    const chunkSize = 0x8000;
    for (let index = 0; index < bytes.length; index += chunkSize) {
      binary += String.fromCharCode(...bytes.subarray(index, index + chunkSize));
    }
    return btoa(binary);
  }

  function base64ToBytes(value) {
    const binary = atob(value);
    const bytes = new Uint8Array(binary.length);
    for (let index = 0; index < binary.length; index += 1) {
      bytes[index] = binary.charCodeAt(index);
    }
    return bytes;
  }

  function openDatabase() {
    return new Promise((resolve, reject) => {
      const request = indexedDB.open(DB_NAME, 1);
      request.onupgradeneeded = () => {
        const database = request.result;
        if (!database.objectStoreNames.contains(STORE_NAME)) {
          database.createObjectStore(STORE_NAME, {keyPath: 'id'});
        }
      };
      request.onsuccess = () => resolve(request.result);
      request.onerror = () => reject(request.error || new Error('Local storage could not open.'));
    });
  }

  async function useStore(mode, operation) {
    const database = await openDatabase();
    return new Promise((resolve, reject) => {
      const transaction = database.transaction(STORE_NAME, mode);
      const store = transaction.objectStore(STORE_NAME);
      let request;
      try {
        request = operation(store);
      } catch (error) {
        database.close();
        reject(error);
        return;
      }
      request.onsuccess = () => resolve(request.result);
      request.onerror = () => reject(request.error || new Error('Local storage failed.'));
      transaction.oncomplete = () => database.close();
      transaction.onerror = () => {
        database.close();
        reject(transaction.error || new Error('Local storage failed.'));
      };
    });
  }

  const getVault = () => useStore('readonly', store => store.get(VAULT_ID));
  const saveVault = vault => useStore('readwrite', store => store.put({...vault, id: VAULT_ID}));
  const deleteVault = () => useStore('readwrite', store => store.delete(VAULT_ID));

  async function deriveKey(passphrase, salt, iterations) {
    const keyMaterial = await crypto.subtle.importKey(
      'raw',
      encoder.encode(passphrase),
      'PBKDF2',
      false,
      ['deriveKey'],
    );
    return crypto.subtle.deriveKey(
      {name: 'PBKDF2', hash: 'SHA-256', salt, iterations},
      keyMaterial,
      {name: 'AES-GCM', length: 256},
      false,
      ['encrypt', 'decrypt'],
    );
  }

  async function encryptData(data, passphrase) {
    const salt = crypto.getRandomValues(new Uint8Array(16));
    const iv = crypto.getRandomValues(new Uint8Array(12));
    const key = await deriveKey(passphrase, salt, ITERATIONS);
    const ciphertext = await crypto.subtle.encrypt(
      {name: 'AES-GCM', iv, additionalData: encoder.encode(FORMAT)},
      key,
      encoder.encode(JSON.stringify(data)),
    );
    return {
      format: FORMAT,
      version: VERSION,
      created_at: new Date().toISOString(),
      source: 'copy',
      kdf: {
        name: 'PBKDF2',
        hash: 'SHA-256',
        iterations: ITERATIONS,
        salt: bytesToBase64(salt),
      },
      cipher: {name: 'AES-GCM', iv: bytesToBase64(iv)},
      ciphertext: bytesToBase64(new Uint8Array(ciphertext)),
    };
  }

  function validateVault(vault) {
    const iterations = Number(vault?.kdf?.iterations);
    if (
      vault?.format !== FORMAT ||
      vault?.version !== VERSION ||
      vault?.kdf?.name !== 'PBKDF2' ||
      vault?.kdf?.hash !== 'SHA-256' ||
      vault?.cipher?.name !== 'AES-GCM' ||
      !vault?.kdf?.salt ||
      !vault?.cipher?.iv ||
      !vault?.ciphertext ||
      !Number.isInteger(iterations) ||
      iterations < 100000 ||
      iterations > 1000000
    ) {
      throw new Error('This is not a supported RATi vault file.');
    }
    return vault;
  }

  async function decryptVault(vault, passphrase) {
    validateVault(vault);
    const salt = base64ToBytes(vault.kdf.salt);
    const iv = base64ToBytes(vault.cipher.iv);
    const key = await deriveKey(passphrase, salt, vault.kdf.iterations);
    const cleartext = await crypto.subtle.decrypt(
      {name: 'AES-GCM', iv, additionalData: encoder.encode(FORMAT)},
      key,
      base64ToBytes(vault.ciphertext),
    );
    return JSON.parse(decoder.decode(cleartext));
  }

  function downloadJson(value, filename) {
    const url = URL.createObjectURL(new Blob(
      [JSON.stringify(value, null, 2)],
      {type: 'application/json'},
    ));
    const link = document.createElement('a');
    link.href = url;
    link.download = filename;
    document.body.appendChild(link);
    link.click();
    link.remove();
    setTimeout(() => URL.revokeObjectURL(url), 0);
  }

  function vaultFilename(vault) {
    const date = String(vault.created_at || new Date().toISOString()).slice(0, 10);
    return `rati-data-${date}.rati-data`;
  }

  async function refreshLocalState() {
    try {
      const vault = await getVault();
      const hasVault = Boolean(vault);
      localActions.hidden = !hasVault;
      localChoice.classList.toggle('active', hasVault);
      if (hasVault) {
        localLabel.textContent = vault.source === 'moved' ? 'Local-only vault saved' : 'Encrypted vault saved';
        localHelp.textContent = `Saved ${new Date(vault.created_at).toLocaleString()}. Passphrase required.`;
      } else {
        localLabel.textContent = 'No local copy';
        localHelp.textContent = 'Save or import an encrypted vault on this browser.';
      }
    } catch (error) {
      localLabel.textContent = 'Local vault unavailable';
      localHelp.textContent = 'This browser did not allow private local storage.';
      setStatus(error.message || 'Local storage is unavailable.', 'error');
    }
  }

  function openCreateDialog(mode) {
    createDialog.dataset.mode = mode;
    const moving = mode === 'move';
    byId('createVaultTitle').textContent = moving ? 'Move data to this device' : 'Save a private copy';
    byId('createVaultDescription').textContent = moving
      ? 'RATi will encrypt, save, and check the local vault before asking the server to remove your saved work.'
      : 'Choose a passphrase. It never leaves this device, and RATi cannot recover it.';
    byId('moveDataCheck').hidden = !moving;
    byId('moveDataConfirmed').required = moving;
    byId('moveDataConfirmed').checked = false;
    byId('createVaultSubmit').textContent = moving ? 'Save, check, then remove Swarm copy' : 'Save encrypted copy';
    byId('createVaultStatus').textContent = '';
    createDialog.showModal();
    byId('vaultPassphrase').focus();
  }

  byId('copyToDevice')?.addEventListener('click', () => openCreateDialog('copy'));
  byId('moveToDevice')?.addEventListener('click', () => openCreateDialog('move'));

  byId('createVaultForm')?.addEventListener('submit', async event => {
    event.preventDefault();
    const form = event.currentTarget;
    const button = byId('createVaultSubmit');
    const formStatus = byId('createVaultStatus');
    const passphrase = byId('vaultPassphrase').value;
    const repeated = byId('vaultPassphraseAgain').value;
    const moving = createDialog.dataset.mode === 'move';
    formStatus.textContent = '';
    if (passphrase.length < 12) {
      formStatus.textContent = 'Use at least 12 characters.';
      return;
    }
    if (passphrase !== repeated) {
      formStatus.textContent = 'The passphrases do not match.';
      return;
    }
    if (moving && !byId('moveDataConfirmed').checked) {
      formStatus.textContent = 'Confirm that the Swarm copy can be removed.';
      return;
    }

    button.disabled = true;
    try {
      formStatus.textContent = 'Preparing your data…';
      const response = await fetch('/api/account/export', {
        headers: {'Accept': 'application/json'},
        cache: 'no-store',
      });
      const exported = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(exported.detail || 'RATi could not prepare your data.');

      formStatus.textContent = 'Encrypting on this device…';
      const vault = await encryptData(exported, passphrase);
      await saveVault(vault);
      const savedVault = await getVault();
      const checked = await decryptVault(savedVault, passphrase);
      if (checked.exported_at !== exported.exported_at) {
        throw new Error('The saved vault could not be checked. Swarm data was not changed.');
      }

      if (moving) {
        formStatus.textContent = 'Local vault checked. Removing the Swarm copy…';
        const deleteResponse = await fetch('/api/account/data/delete-cloud-copy', {
          method: 'POST',
          headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({confirmation: 'MOVE MY DATA'}),
        });
        const result = await deleteResponse.json().catch(() => ({}));
        if (!deleteResponse.ok) {
          throw new Error(result.detail || 'The local vault is safe, but the Swarm copy was not removed.');
        }
        await saveVault({...savedVault, source: 'moved', cloud_deleted_at: new Date().toISOString()});
        sessionStorage.setItem(NOTICE_KEY, 'Your checked vault is on this device. Saved Swarm items were removed.');
        location.reload();
        return;
      }

      form.reset();
      createDialog.close();
      await refreshLocalState();
      setStatus('Encrypted copy saved and checked on this device.', 'success');
    } catch (error) {
      formStatus.textContent = error.message || 'Could not save the encrypted copy.';
    } finally {
      button.disabled = false;
    }
  });

  byId('openLocalVault')?.addEventListener('click', () => {
    unlockedData = null;
    byId('vaultPreview').hidden = true;
    byId('openVaultStatus').textContent = '';
    byId('openVaultForm').hidden = false;
    openDialog.showModal();
    byId('openVaultPassphrase').focus();
  });

  function listCount(data, keys) {
    return keys.reduce((total, key) => total + (Array.isArray(data[key]) ? data[key].length : 0), 0);
  }

  byId('openVaultForm')?.addEventListener('submit', async event => {
    event.preventDefault();
    const formStatus = byId('openVaultStatus');
    const button = event.currentTarget.querySelector('button[type="submit"]');
    button.disabled = true;
    formStatus.textContent = 'Unlocking…';
    try {
      const vault = await getVault();
      if (!vault) throw new Error('No local vault was found.');
      unlockedData = await decryptVault(vault, byId('openVaultPassphrase').value);
      byId('openVaultForm').hidden = true;
      const exportedAt = unlockedData.exported_at ? new Date(unlockedData.exported_at).toLocaleString() : 'Unknown date';
      byId('vaultPreviewMeta').textContent = `Snapshot from ${exportedAt}`;
      const groups = [
        ['Posts and Calls', ['comments', 'community_calls', 'sports_picks', 'signals', 'reports_submitted']],
        ['Private work', ['watchlist', 'positions', 'cases', 'case_revisions', 'case_updates', 'case_outcomes']],
        ['Research', ['research', 'research_stages', 'flash_forecasts', 'sports_ai_forecasts', 'flash_forecast_outcomes', 'flash_evaluation_events', 'flash_report_requests']],
        ['Account records', ['passkeys', 'sessions', 'comment_avatar', 'public_thread_aliases', 'caller_identities', 'caller_identity_claims', 'flash_wallet', 'flash_transactions']],
      ];
      const previewGroups = byId('vaultPreviewGroups');
      previewGroups.replaceChildren();
      groups.forEach(([label, keys]) => {
        const row = document.createElement('div');
        const name = document.createElement('span');
        const count = document.createElement('strong');
        name.textContent = label;
        count.textContent = String(listCount(unlockedData, keys));
        row.append(name, count);
        previewGroups.appendChild(row);
      });
      byId('vaultPreview').hidden = false;
      byId('openVaultPassphrase').value = '';
    } catch (error) {
      formStatus.textContent = 'Could not unlock. Check your passphrase and vault file.';
    } finally {
      button.disabled = false;
    }
  });

  byId('downloadReadableVault')?.addEventListener('click', () => {
    if (!unlockedData) return;
    const date = String(unlockedData.exported_at || new Date().toISOString()).slice(0, 10);
    downloadJson(unlockedData, `rati-readable-data-${date}.json`);
  });

  byId('downloadLocalVault')?.addEventListener('click', async () => {
    try {
      const vault = await getVault();
      if (!vault) throw new Error('No local vault was found.');
      const {id, ...portableVault} = vault;
      downloadJson(portableVault, vaultFilename(vault));
      setStatus('Encrypted vault file downloaded.', 'success');
    } catch (error) {
      setStatus(error.message || 'Could not download the local vault.', 'error');
    }
  });

  byId('importLocalVault')?.addEventListener('click', () => byId('localVaultFile').click());
  byId('localVaultFile')?.addEventListener('change', async event => {
    const file = event.currentTarget.files?.[0];
    event.currentTarget.value = '';
    if (!file) return;
    if (file.size > 50 * 1024 * 1024) {
      setStatus('This vault is larger than the 50 MB import limit.', 'error');
      return;
    }
    try {
      if (await getVault()) {
        throw new Error('Delete the current local copy before importing another vault.');
      }
      const vault = validateVault(JSON.parse(await file.text()));
      await saveVault({...vault, source: vault.source === 'moved' ? 'moved' : 'imported'});
      await refreshLocalState();
      setStatus('Encrypted vault imported. Open it with its passphrase.', 'success');
    } catch (error) {
      setStatus(error.message || 'Could not import this vault file.', 'error');
    }
  });

  byId('openLocalDelete')?.addEventListener('click', () => {
    byId('localDeleteConfirmation').value = '';
    byId('localDeleteStatus').textContent = '';
    localDeleteDialog.showModal();
    byId('localDeleteConfirmation').focus();
  });

  byId('localDeleteForm')?.addEventListener('submit', async event => {
    event.preventDefault();
    if (byId('localDeleteConfirmation').value !== 'DELETE LOCAL COPY') {
      byId('localDeleteStatus').textContent = 'Type DELETE LOCAL COPY exactly.';
      return;
    }
    try {
      await deleteVault();
      unlockedData = null;
      localDeleteDialog.close();
      await refreshLocalState();
      setStatus('The local vault was permanently deleted.', 'success');
    } catch (error) {
      byId('localDeleteStatus').textContent = error.message || 'Could not delete the local vault.';
    }
  });

  document.querySelectorAll('[data-close-dialog]').forEach(button => {
    button.addEventListener('click', () => {
      const dialog = button.closest('dialog');
      if (dialog === openDialog) {
        unlockedData = null;
        byId('vaultPreview').hidden = true;
        byId('openVaultForm').reset();
      }
      dialog.close();
    });
  });
  [createDialog, openDialog, localDeleteDialog].forEach(dialog => {
    dialog.addEventListener('click', event => {
      if (event.target === dialog) dialog.close();
    });
  });

  const notice = sessionStorage.getItem(NOTICE_KEY);
  if (notice) {
    sessionStorage.removeItem(NOTICE_KEY);
    setStatus(notice, 'success');
  }
  if (!window.crypto?.subtle || !window.indexedDB) {
    localLabel.textContent = 'Local vault unavailable';
    localHelp.textContent = 'Use a current browser with private storage and encryption support.';
    localActions.hidden = true;
    byId('copyToDevice')?.setAttribute('disabled', '');
    byId('moveToDevice')?.setAttribute('disabled', '');
    byId('importLocalVault')?.setAttribute('disabled', '');
    return;
  }
  refreshLocalState();
})();
