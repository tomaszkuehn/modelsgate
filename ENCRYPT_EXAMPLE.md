# Encryption Example — Kotlin (Android)

Complete client implementation with hybrid RSA+AES-GCM encryption.

```kotlin
package com.example.aibackend

import android.util.Base64
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import okhttp3.*
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.RequestBody.Companion.toRequestBody
import org.json.JSONObject
import java.security.KeyFactory
import java.security.spec.X509EncodedKeySpec
import javax.crypto.Cipher
import javax.crypto.KeyGenerator
import javax.crypto.spec.GCMParameterSpec
import javax.crypto.spec.SecretKeySpec

// ── Data classes ─────────────────────────────────────────────────────────

data class RegisterResponse(val client_id: String, val plan: String)
data class ApiResponse(val id: String, val task_type: String?, val model: String,
                       val content: List<ContentBlock>, val error: String?)
data class ContentBlock(val type: String, val text: String?, val image: String?)

// ── Client ───────────────────────────────────────────────────────────────

class AIClient(private val baseUrl: String = "http://10.0.2.2:8000") {

    private val client = OkHttpClient()
    private var publicKey: java.security.PublicKey? = null
    private val jsonMedia = "application/json".toMediaType()

    // ── 1. Fetch public key ──────────────────────────────────────────

    suspend fun fetchPublicKey() = withContext(Dispatchers.IO) {
        val request = Request.Builder().url("$baseUrl/api/v1/public-key").get().build()
        val response = client.newCall(request).execute()
        val json = JSONObject(response.body!!.string())
        val pem = json.getString("public_key")
            .replace("-----BEGIN PUBLIC KEY-----", "")
            .replace("-----END PUBLIC KEY-----", "")
            .replace("\n", "")
        val keyBytes = Base64.decode(pem, Base64.DEFAULT)
        publicKey = KeyFactory.getInstance("RSA")
            .generatePublic(X509EncodedKeySpec(keyBytes))
    }

    // ── 2. Register client ───────────────────────────────────────────

    suspend fun register(): RegisterResponse = withContext(Dispatchers.IO) {
        val request = Request.Builder().url("$baseUrl/api/v1/register").get().build()
        val json = JSONObject(client.newCall(request).execute().body!!.string())
        RegisterResponse(json.getString("client_id"), json.getString("plan"))
    }

    // ── 3. Send encrypted request ────────────────────────────────────

    suspend fun sendRequest(
        taskType: String,
        messages: List<JSONObject>,
        clientId: String,
        model: String? = null,
        parameters: JSONObject? = null,
    ): ApiResponse = withContext(Dispatchers.IO) {

        if (publicKey == null) fetchPublicKey()

        // Build payload
        val payload = JSONObject().apply {
            put("task_type", taskType)
            put("client_id", clientId)
            put("messages", org.json.JSONArray(messages))
            if (model != null) put("model", model)
            if (parameters != null) put("parameters", parameters)
        }

        // Generate session key + nonce
        val keyGen = KeyGenerator.getInstance("AES").apply { init(256) }
        val sessionKey = keyGen.generateKey()
        val nonce = ByteArray(12).also { java.security.SecureRandom().nextBytes(it) }

        // Encrypt payload with AES-256-GCM
        val aesCipher = Cipher.getInstance("AES/GCM/NoPadding")
        aesCipher.init(Cipher.ENCRYPT_MODE, sessionKey, GCMParameterSpec(128, nonce))
        val ciphertext = aesCipher.doFinal(payload.toString().toByteArray(Charsets.UTF_8))

        // Encrypt session key with RSA-OAEP
        val rsaCipher = Cipher.getInstance("RSA/ECB/OAEPWithSHA-256AndMGF1Padding")
        rsaCipher.init(Cipher.ENCRYPT_MODE, publicKey)
        val encryptedKey = rsaCipher.doFinal(sessionKey.encoded)

        // Send envelope
        val envelope = JSONObject().apply {
            put("encrypted_key", Base64.encodeToString(encryptedKey, Base64.NO_WRAP))
            put("encrypted_payload", Base64.encodeToString(ciphertext, Base64.NO_WRAP))
            put("nonce", Base64.encodeToString(nonce, Base64.NO_WRAP))
        }

        val requestBody = envelope.toString().toRequestBody(jsonMedia)
        val request = Request.Builder()
            .url("$baseUrl/api/v1/request")
            .post(requestBody)
            .build()

        val response = client.newCall(request).execute()
        val responseJson = JSONObject(response.body!!.string())

        // Decrypt response
        val respNonce = Base64.decode(responseJson.getString("nonce"), Base64.NO_WRAP)
        val respPayload = Base64.decode(responseJson.getString("encrypted_payload"), Base64.NO_WRAP)

        aesCipher.init(Cipher.DECRYPT_MODE, sessionKey, GCMParameterSpec(128, respNonce))
        val plaintext = aesCipher.doFinal(respPayload)
        val result = JSONObject(String(plaintext, Charsets.UTF_8))

        // Parse content blocks
        val contentArray = result.getJSONArray("content")
        val blocks = (0 until contentArray.length()).map { i ->
            val block = contentArray.getJSONObject(i)
            ContentBlock(
                type = block.getString("type"),
                text = block.optString("text", null),
                image = block.optString("image", null),
            )
        }

        ApiResponse(
            id = result.getString("id"),
            task_type = result.optString("task_type", null),
            model = result.getString("model"),
            content = blocks,
            error = result.optString("error", null),
        )
    }
}

// ── Helper: build a message ──────────────────────────────────────────────

fun textMessage(text: String, role: String = "user"): JSONObject =
    JSONObject().apply {
        put("role", role)
        put("content", org.json.JSONArray().apply {
            put(JSONObject().apply { put("type", "text"); put("text", text) })
        })
    }

fun visionMessage(text: String, imageBase64: String, role: String = "user"): JSONObject =
    JSONObject().apply {
        put("role", role)
        put("content", org.json.JSONArray().apply {
            put(JSONObject().apply { put("type", "text"); put("text", text) })
            put(JSONObject().apply { put("type", "image"); put("image", imageBase64) })
        })
    }

// ── Usage ────────────────────────────────────────────────────────────────

// val client = AIClient("http://10.0.2.2:8000")
// val reg = client.register()
// println("Client ID: ${reg.client_id}")
//
// val response = client.sendRequest(
//     taskType = "chat_with_context",
//     messages = listOf(textMessage("What is the capital of France?")),
//     clientId = reg.client_id,
// )
// println("Model: ${response.model}")
// println("Text: ${response.content.firstOrNull()?.text}")
// println("Error: ${response.error}")
```

## Encryption Flow

```
1. GET /api/v1/public-key     → RSA public key (PEM)
2. GET /api/v1/register       → {client_id, plan: "free"}
3. Generate session_key (AES-256) + nonce (12 bytes)
4. Encrypt payload JSON with AES-256-GCM
5. Encrypt session_key with RSA-OAEP (server's public key)
6. POST /api/v1/request {encrypted_key, encrypted_payload, nonce}
7. Decrypt response with same session_key + response nonce
8. (async jobs) Poll GET /api/v1/jobs/{job_id} and cancel POST /api/v1/jobs/{job_id}/cancel
   — both responses decrypt with the SAME session_key from step 3
```

## Common Mistakes (BAD_DECRYPT)

| Mistake | Fix |
|---------|-----|
| Using request nonce to decrypt response | Use `responseJson.getString("nonce")` |
| Re-generating session key for response | Keep the same `sessionKey` object |
| Decrypting job poll/cancel responses | Reuse the original request's `sessionKey` — poll & cancel envelopes are sealed with it |
| Not stripping PEM headers | Remove `-----BEGIN/END PUBLIC KEY-----` and newlines |
| Wrong Base64 flags | Use `Base64.NO_WRAP` |
