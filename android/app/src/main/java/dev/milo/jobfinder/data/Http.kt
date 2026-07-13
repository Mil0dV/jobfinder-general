package dev.milo.jobfinder.data

import okhttp3.MediaType.Companion.toMediaType
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.RequestBody.Companion.toRequestBody
import okhttp3.Response
import java.util.concurrent.TimeUnit

object Http {
    const val USER_AGENT = "Mozilla/5.0 (X11; Linux x86_64) JobFinder/1.0"

    val client: OkHttpClient = OkHttpClient.Builder()
        .connectTimeout(15, TimeUnit.SECONDS)
        .readTimeout(30, TimeUnit.SECONDS)
        .followRedirects(true)
        .build()

    fun get(url: String, headers: Map<String, String> = emptyMap()): String? {
        return try {
            request(url, headers).use { resp ->
                if (!resp.isSuccessful) null else resp.body?.string()
            }
        } catch (e: Exception) {
            null
        }
    }

    fun request(url: String, headers: Map<String, String> = emptyMap()): Response {
        val builder = Request.Builder().url(url).header("User-Agent", USER_AGENT)
        headers.forEach { (k, v) -> builder.header(k, v) }
        return client.newCall(builder.build()).execute()
    }

    fun postJson(url: String, body: String, headers: Map<String, String> = emptyMap()): String? {
        return try {
            val builder = Request.Builder()
                .url(url)
                .header("User-Agent", USER_AGENT)
                .post(body.toRequestBody("application/json".toMediaType()))
            headers.forEach { (k, v) -> builder.header(k, v) }
            client.newCall(builder.build()).execute().use { resp ->
                if (!resp.isSuccessful) null else resp.body?.string()
            }
        } catch (e: Exception) {
            null
        }
    }
}
