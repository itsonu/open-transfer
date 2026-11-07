package io.github.itsonu.opentransfer

import android.security.keystore.KeyGenParameterSpec
import android.security.keystore.KeyProperties
import android.util.Base64
import java.security.KeyStore
import javax.crypto.Cipher
import javax.crypto.KeyGenerator
import javax.crypto.SecretKey
import javax.crypto.spec.GCMParameterSpec

/**
 * Wraps Python's vault key (open_transfer/keystore.py) with an AES key that lives in the
 * Android Keystore: it can't be read out of the phone, so a copied state folder (or a
 * backup, though backups are off) is useless without this phone. Called through Chaquopy.
 */
object KeyVault {
    private const val ALIAS = "open-transfer-vault"
    private const val STORE = "AndroidKeyStore"
    private const val TRANSFORM = "AES/GCM/NoPadding"

    @JvmStatic
    fun wrap(plainB64: String): String {
        val cipher = Cipher.getInstance(TRANSFORM)
        cipher.init(Cipher.ENCRYPT_MODE, key()) // the Keystore picks the nonce
        val sealed = cipher.doFinal(Base64.decode(plainB64, Base64.NO_WRAP))
        return Base64.encodeToString(cipher.iv + sealed, Base64.NO_WRAP)
    }

    @JvmStatic
    fun unwrap(wrappedB64: String): String {
        val blob = Base64.decode(wrappedB64, Base64.NO_WRAP)
        val cipher = Cipher.getInstance(TRANSFORM)
        cipher.init(Cipher.DECRYPT_MODE, key(), GCMParameterSpec(128, blob, 0, 12))
        val plain = cipher.doFinal(blob, 12, blob.size - 12)
        return Base64.encodeToString(plain, Base64.NO_WRAP)
    }

    @Synchronized
    private fun key(): SecretKey {
        val store = KeyStore.getInstance(STORE).apply { load(null) }
        (store.getKey(ALIAS, null) as? SecretKey)?.let { return it }
        val generator = KeyGenerator.getInstance(KeyProperties.KEY_ALGORITHM_AES, STORE)
        generator.init(
            KeyGenParameterSpec.Builder(ALIAS, KeyProperties.PURPOSE_ENCRYPT or KeyProperties.PURPOSE_DECRYPT)
                .setBlockModes(KeyProperties.BLOCK_MODE_GCM)
                .setEncryptionPaddings(KeyProperties.ENCRYPTION_PADDING_NONE)
                .setKeySize(256)
                .build(),
        )
        return generator.generateKey()
    }
}
