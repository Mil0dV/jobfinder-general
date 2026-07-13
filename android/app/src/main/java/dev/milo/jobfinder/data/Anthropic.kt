package dev.milo.jobfinder.data

import okhttp3.MediaType.Companion.toMediaType
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.RequestBody.Companion.toRequestBody
import org.json.JSONArray
import org.json.JSONObject
import java.util.concurrent.TimeUnit

/** Thin Anthropic Messages API client (blocking + SSE streaming) over OkHttp. */
object Anthropic {
    private const val API_URL = "https://api.anthropic.com/v1/messages"
    private const val VERSION = "2023-06-01"

    const val GENERATION_MODEL = "claude-sonnet-4-6"
    const val FAST_MODEL = "claude-haiku-4-5"

    private val client: OkHttpClient = OkHttpClient.Builder()
        .connectTimeout(15, TimeUnit.SECONDS)
        .readTimeout(180, TimeUnit.SECONDS)
        .build()

    private fun buildRequest(body: JSONObject): Request = Request.Builder()
        .url(API_URL)
        .header("x-api-key", Keys.ANTHROPIC_API_KEY)
        .header("anthropic-version", VERSION)
        .post(body.toString().toRequestBody("application/json".toMediaType()))
        .build()

    /** Blocking completion; returns the text content or throws with the API error. */
    fun complete(
        model: String,
        maxTokens: Int,
        system: String? = null,
        user: String,
        outputSchema: JSONObject? = null,
    ): String {
        val body = JSONObject()
            .put("model", model)
            .put("max_tokens", maxTokens)
            .put("messages", JSONArray().put(JSONObject().put("role", "user").put("content", user)))
        if (system != null) body.put("system", system)
        if (outputSchema != null) {
            body.put(
                "output_config",
                JSONObject().put(
                    "format",
                    JSONObject().put("type", "json_schema").put("schema", outputSchema)
                )
            )
        }
        client.newCall(buildRequest(body)).execute().use { resp ->
            val text = resp.body?.string() ?: ""
            if (!resp.isSuccessful) {
                val msg = try {
                    JSONObject(text).optJSONObject("error")?.optString("message")
                } catch (e: Exception) {
                    null
                }
                throw RuntimeException("API ${resp.code}: ${msg ?: text.take(200)}")
            }
            val content = JSONObject(text).optJSONArray("content") ?: return ""
            for (i in 0 until content.length()) {
                val block = content.getJSONObject(i)
                if (block.optString("type") == "text") return block.optString("text")
            }
            return ""
        }
    }

    /**
     * Streaming completion: calls [onDelta] per text chunk, returns the full text.
     * Check [shouldStop] between chunks to support cancellation.
     */
    fun stream(
        model: String,
        maxTokens: Int,
        system: String?,
        user: String,
        shouldStop: () -> Boolean = { false },
        onDelta: (String) -> Unit,
    ): String = stream(
        model, maxTokens, system,
        JSONArray().put(JSONObject().put("role", "user").put("content", user)),
        shouldStop, onDelta,
    )

    /** Streaming completion over an explicit message history (multi-turn). */
    fun stream(
        model: String,
        maxTokens: Int,
        system: String?,
        messages: JSONArray,
        shouldStop: () -> Boolean = { false },
        onDelta: (String) -> Unit,
    ): String {
        val body = JSONObject()
            .put("model", model)
            .put("max_tokens", maxTokens)
            .put("stream", true)
            .put("messages", messages)
        if (system != null) body.put("system", system)

        val full = StringBuilder()
        client.newCall(buildRequest(body)).execute().use { resp ->
            if (!resp.isSuccessful) {
                val text = resp.body?.string() ?: ""
                val msg = try {
                    JSONObject(text).optJSONObject("error")?.optString("message")
                } catch (e: Exception) {
                    null
                }
                throw RuntimeException("API ${resp.code}: ${msg ?: text.take(200)}")
            }
            val source = resp.body?.source() ?: return ""
            while (!source.exhausted()) {
                if (shouldStop()) break
                val line = source.readUtf8Line() ?: break
                if (!line.startsWith("data:")) continue
                val payload = line.removePrefix("data:").trim()
                if (payload.isEmpty() || payload == "[DONE]") continue
                val event = try {
                    JSONObject(payload)
                } catch (e: Exception) {
                    continue
                }
                if (event.optString("type") == "content_block_delta") {
                    val delta = event.optJSONObject("delta") ?: continue
                    if (delta.optString("type") == "text_delta") {
                        val t = delta.optString("text")
                        full.append(t)
                        onDelta(t)
                    }
                }
            }
        }
        return full.toString()
    }
}
