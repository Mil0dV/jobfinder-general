package dev.milo.jobfinder.data

import org.json.JSONArray
import org.json.JSONObject
import java.time.Instant
import java.time.ZoneOffset
import java.time.format.DateTimeFormatter

/**
 * The ten job sources, ported from agent.py. Each returns title-filtered,
 * location-filtered jobs; failures return an empty list (the search goes on).
 */
object Sources {

    val ALL: List<Pair<String, (String) -> List<Job>>> = listOf(
        "arbeitnow" to ::arbeitnow,
        "remoteok" to ::remoteok,
        "adzuna" to ::adzuna,
        "greenhouse" to ::greenhouse,
        "lever" to ::lever,
        "climatetechlist" to ::climatetechlist,
        "climatebase" to ::climatebase,
        "jobsincarbon" to ::jobsincarbon,
        "remoteimpact" to ::remoteimpact,
        "cdrjobs" to ::cdrjobs,
    )

    private fun stripHtml(s: String): String = s.replace(Regex("<[^>]+>"), " ")

    private fun keep(job: Job, query: String): Boolean =
        Profile.titleMatches(job.title, query) && Profile.matchesLocations(job)

    // --- Arbeitnow (EU/DACH, free API) -----------------------------------------

    fun arbeitnow(query: String): List<Job> {
        val body = Http.get("https://www.arbeitnow.com/api/job-board-api") ?: return emptyList()
        val data = JSONObject(body).optJSONArray("data") ?: return emptyList()
        val jobs = mutableListOf<Job>()
        for (i in 0 until data.length()) {
            val j = data.getJSONObject(i)
            val job = Job.make(
                title = j.optString("title"),
                company = j.optString("company_name"),
                url = j.optString("url"),
                location = j.optString("location"),
                postedDate = epochToDate(j.optLong("created_at", 0)),
                wfhPolicy = if (j.optBoolean("remote")) "Remote" else "On-site",
                snippet = stripHtml(j.optString("description")),
                source = "arbeitnow",
            )
            if (keep(job, query)) jobs.add(job)
        }
        return jobs
    }

    private fun epochToDate(epoch: Long): String =
        if (epoch <= 0) "" else Instant.ofEpochSecond(epoch).atZone(ZoneOffset.UTC)
            .format(DateTimeFormatter.ISO_LOCAL_DATE)

    // --- RemoteOK ----------------------------------------------------------------

    fun remoteok(query: String): List<Job> {
        val body = Http.get("https://remoteok.com/api") ?: return emptyList()
        val arr = JSONArray(body)
        val jobs = mutableListOf<Job>()
        for (i in 0 until arr.length()) {
            val j = arr.optJSONObject(i) ?: continue
            if (!j.has("position")) continue
            val salMin = j.optLong("salary_min", 0)
            val salMax = j.optLong("salary_max", 0)
            val salary = if (salMin > 0 && salMax > 0) "$%,d - $%,d".format(salMin, salMax) else null
            val job = Job.make(
                title = j.optString("position"),
                company = j.optString("company"),
                url = j.optString("url"),
                location = j.optString("location").ifBlank { "Remote" },
                salary = salary,
                postedDate = j.optString("date").take(10),
                wfhPolicy = "Remote",
                snippet = stripHtml(j.optString("description")),
                source = "remoteok",
            )
            if (keep(job, query)) jobs.add(job)
        }
        return jobs
    }

    // --- Adzuna (needs API key; profile mode fans out over CH/DE/NL) ---------------

    fun adzuna(query: String): List<Job> {
        if (Keys.ADZUNA_APP_ID.isBlank() || Keys.ADZUNA_API_KEY.isBlank()) return emptyList()
        val seeds = if (query == Profile.PROFILE_QUERY) Profile.PROFILE_QUERY_SEEDS else listOf(query)
        val countries = listOf("ch", "de", "nl")
        val jobs = mutableListOf<Job>()
        val seen = mutableSetOf<String>()
        for (country in countries) {
            for (seed in seeds) {
                val url = "https://api.adzuna.com/v1/api/jobs/$country/search/1" +
                    "?app_id=${Keys.ADZUNA_APP_ID}&app_key=${Keys.ADZUNA_API_KEY}" +
                    "&what=${java.net.URLEncoder.encode(seed, "UTF-8")}" +
                    "&results_per_page=50&max_days_old=60&sort_by=date"
                val body = Http.get(url) ?: continue
                val results = try {
                    JSONObject(body).optJSONArray("results") ?: continue
                } catch (e: Exception) {
                    continue
                }
                for (i in 0 until results.length()) {
                    val j = results.getJSONObject(i)
                    val jobUrl = j.optString("redirect_url")
                    if (jobUrl in seen) continue
                    seen.add(jobUrl)
                    val desc = j.optString("description")
                    val title = j.optString("title")
                    val salMin = j.optDouble("salary_min", 0.0)
                    val salary = if (salMin > 0) {
                        "%,d - %,d".format(salMin.toLong(), j.optDouble("salary_max", 0.0).toLong())
                    } else null
                    val job = Job.make(
                        title = title,
                        company = j.optJSONObject("company")?.optString("display_name"),
                        url = jobUrl,
                        location = j.optJSONObject("location")?.optString("display_name"),
                        salary = salary,
                        postedDate = j.optString("created").take(10),
                        wfhPolicy = if ("remote" in (title + desc).lowercase()) "Remote" else "On-site",
                        snippet = desc,
                        source = "adzuna",
                    )
                    if (keep(job, query)) jobs.add(job)
                }
            }
        }
        return jobs
    }

    // --- Greenhouse & Lever (curated climate companies, verified 2026-07) ----------

    private val GREENHOUSE_COMPANIES = listOf(
        "watershed", "overstory", "patch", "ecoworks", "redwoodmaterials",
        "eh2", "camusenergy", "carbonfuture",
    )
    private val LEVER_COMPANIES = listOf("pachama", "envelio", "voltus")

    fun greenhouse(query: String): List<Job> {
        val jobs = mutableListOf<Job>()
        for (slug in GREENHOUSE_COMPANIES) {
            val body = Http.get("https://boards-api.greenhouse.io/v1/boards/$slug/jobs") ?: continue
            val arr = try {
                JSONObject(body).optJSONArray("jobs") ?: continue
            } catch (e: Exception) {
                continue
            }
            for (i in 0 until arr.length()) {
                val j = arr.getJSONObject(i)
                val loc = j.optJSONObject("location")?.optString("name") ?: ""
                val job = Job.make(
                    title = j.optString("title"),
                    company = slug.replace("-", " ").replaceFirstChar { it.uppercase() },
                    url = j.optString("absolute_url"),
                    location = loc,
                    postedDate = j.optString("updated_at").take(10),
                    wfhPolicy = if ("remote" in loc.lowercase()) "Remote" else "On-site",
                    source = "greenhouse",
                )
                if (keep(job, query)) jobs.add(job)
            }
        }
        return jobs
    }

    fun lever(query: String): List<Job> {
        val jobs = mutableListOf<Job>()
        for (slug in LEVER_COMPANIES) {
            val body = Http.get("https://api.lever.co/v0/postings/$slug?mode=json") ?: continue
            val arr = try {
                JSONArray(body)
            } catch (e: Exception) {
                continue
            }
            for (i in 0 until arr.length()) {
                val j = arr.optJSONObject(i) ?: continue
                val loc = j.optJSONObject("categories")?.optString("location") ?: ""
                val ts = j.optLong("createdAt", 0)
                val workplace = j.optString("workplaceType")
                val job = Job.make(
                    title = j.optString("text"),
                    company = slug.replace("-", " ").replaceFirstChar { it.uppercase() },
                    url = j.optString("hostedUrl"),
                    location = loc,
                    postedDate = if (ts > 0) epochToDate(ts / 1000) else "",
                    wfhPolicy = if (workplace == "remote") "Remote"
                    else workplace.replaceFirstChar { it.uppercase() }.ifBlank { "Unknown" },
                    snippet = j.optString("descriptionPlain"),
                    source = "lever",
                )
                if (keep(job, query)) jobs.add(job)
            }
        }
        return jobs
    }

    // --- ClimateTechList (Airtable shared view, ~10k climate jobs) ------------------

    private const val CTL_URL =
        "https://airtable.com/embed/appo9WyVgYvpap792/shrbAhhxKqvWjaJMs"

    fun climatetechlist(query: String): List<Job> {
        val html: String
        val cookies: String
        try {
            Http.request(CTL_URL).use { resp ->
                if (!resp.isSuccessful) return emptyList()
                html = resp.body?.string() ?: return emptyList()
                cookies = resp.headers("Set-Cookie").joinToString("; ") { it.substringBefore(";") }
            }
        } catch (e: Exception) {
            return emptyList()
        }

        val urlMatch = Regex("urlWithParams[\"\\s:]*\"(.*?)\"").find(html) ?: return emptyList()
        val apiUrl = "https://airtable.com" +
            urlMatch.groupValues[1].replace("\\u002F", "/").replace("\\/", "/")

        val headers = mutableMapOf(
            "X-Requested-With" to "XMLHttpRequest",
            "x-airtable-inter-service-client" to "webClient",
            "x-time-zone" to "Europe/Zurich",
        )
        Regex("\"x-airtable-application-id\"\\s*:\\s*\"([^\"]*)\"").find(html)
            ?.let { headers["x-airtable-application-id"] = it.groupValues[1] }
        if (cookies.isNotBlank()) headers["Cookie"] = cookies

        val body = Http.get(apiUrl, headers) ?: return emptyList()
        val table = try {
            JSONObject(body).optJSONObject("data")?.optJSONObject("table") ?: return emptyList()
        } catch (e: Exception) {
            return emptyList()
        }

        val colByName = mutableMapOf<String, String>()
        val columns = table.optJSONArray("columns") ?: JSONArray()
        for (i in 0 until columns.length()) {
            val c = columns.getJSONObject(i)
            colByName[c.optString("name").lowercase().trim()] = c.optString("id")
        }

        fun findCol(vararg candidates: String): String? {
            for (name in candidates) colByName[name]?.let { return it }
            for (cand in candidates) {
                for ((colName, colId) in colByName) if (cand in colName) return colId
            }
            return null
        }

        val colTitle = findCol("position title", "title", "position")
        val colCompany = findCol("company", "employer")
        val colApply = findCol("apply to job", "apply")
        val colLocation = findCol("job location", "location")
        val colSalary = findCol("salary range (beta)", "salary", "compensation")
        val colDate = findCol("date first listed", "date")
        val colRemote = findCol("remote")

        val jobs = mutableListOf<Job>()
        val rows = table.optJSONArray("rows") ?: JSONArray()
        for (i in 0 until rows.length()) {
            val cells = rows.getJSONObject(i).optJSONObject("cellValuesByColumnId") ?: continue

            fun cell(colId: String?, default: String = ""): String {
                if (colId == null) return default
                val v = cells.opt(colId) ?: return default
                if (v is JSONObject) return v.optString("url")
                return v.toString().trim().ifBlank { default }
            }

            val title = cell(colTitle).replace(Regex("^[✨🌟⭐🔥]+"), "").trim()
            val job = Job.make(
                title = title,
                company = cell(colCompany, "Unknown"),
                url = cell(colApply),
                location = cell(colLocation).substringBefore("\n").trim(),
                salary = cell(colSalary, "Not listed"),
                postedDate = cell(colDate).take(10),
                wfhPolicy = cell(colRemote, "Unknown"),
                source = "climatetechlist",
            )
            if (keep(job, query)) jobs.add(job)
        }
        return jobs
    }

    // --- Climatebase (public search-only Algolia index) ------------------------------

    private const val CB_APP = "8PSNFFQTXQ"
    private const val CB_KEY = "d2ebe27d3cc3d35fea04da7b1b0718a8"

    private val APPLY_SUFFIX_RE = Regex("/(?:apply|application|applicants/new)/?$", RegexOption.IGNORE_CASE)

    // Climatebase's `how_to_apply` is whatever the company's own ATS "Apply" button
    // links to, which is often the application FORM, not the posting (Lever .../apply,
    // Ashby .../application, Greenhouse job boards .../applicants/new) -- ported from
    // agent.py's _strip_apply_suffix.
    private fun stripApplySuffix(url: String): String {
        val cut = url.indexOfAny(charArrayOf('?', '#'))
        val base = if (cut >= 0) url.substring(0, cut) else url
        val rest = if (cut >= 0) url.substring(cut) else ""
        val stripped = APPLY_SUFFIX_RE.replace(base, "")
        return if (stripped == base) url else stripped + rest
    }

    fun climatebase(query: String): List<Job> {
        val endpoint = "https://$CB_APP-dsn.algolia.net/1/indexes/Job_production/query"
        val headers = mapOf(
            "X-Algolia-API-Key" to CB_KEY,
            "X-Algolia-Application-Id" to CB_APP,
        )
        // OR group: jobs in one of the target countries, or remote anywhere.
        val facetFilters = JSONArray().put(
            JSONArray(
                listOf(
                    "countries:Switzerland", "countries:Germany", "countries:Netherlands",
                    "remote_preferences:Remote",
                )
            )
        )
        val seeds = if (query == Profile.PROFILE_QUERY) Profile.PROFILE_QUERY_SEEDS else listOf(query)
        val jobs = mutableListOf<Job>()
        val seen = mutableSetOf<String>()
        for (seed in seeds) {
            val reqBody = JSONObject()
                .put("query", seed)
                .put("hitsPerPage", 50)
                .put("facetFilters", facetFilters)
                .toString()
            val body = Http.postJson(endpoint, reqBody, headers) ?: continue
            val hits = try {
                JSONObject(body).optJSONArray("hits") ?: continue
            } catch (e: Exception) {
                continue
            }
            for (i in 0 until hits.length()) {
                val hit = hits.getJSONObject(i)
                val jobId = hit.optString("objectID")
                if (jobId in seen) continue
                seen.add(jobId)
                var url = hit.optString("how_to_apply")
                if (!url.startsWith("http")) url = "https://climatebase.org/job/$jobId"
                url = stripApplySuffix(url)
                val remotePrefs = hit.optJSONArray("remote_preferences") ?: JSONArray()
                val remoteList = (0 until remotePrefs.length()).map { remotePrefs.getString(it) }
                val locations = hit.optJSONArray("locations") ?: JSONArray()
                val countries = hit.optJSONArray("countries") ?: JSONArray()
                val locStr = (0 until minOf(2, locations.length())).joinToString(", ") {
                    locations.getString(it)
                }.ifBlank {
                    (0 until minOf(2, countries.length())).joinToString(", ") { countries.getString(it) }
                }.ifBlank { if ("Remote" in remoteList) "Remote" else "" }
                val salFrom = hit.optString("salary_from")
                val job = Job.make(
                    title = hit.optString("title"),
                    company = hit.optString("name_of_employer"),
                    url = url,
                    location = locStr,
                    salary = if (salFrom.isNotBlank() && salFrom != "null")
                        "$salFrom - ${hit.optString("salary_to")}" else null,
                    postedDate = hit.optString("activation_date").take(10),
                    wfhPolicy = remoteList.joinToString(", ").ifBlank { "Unknown" },
                    snippet = stripHtml(hit.optString("description")),
                    source = "climatebase",
                )
                if (keep(job, query)) jobs.add(job)
            }
        }
        return jobs
    }

    // --- Jobs in Carbon (niceboard sitemap) -------------------------------------------

    fun jobsincarbon(query: String): List<Job> {
        val xml = Http.get("https://jobsincarbon.niceboard.co/sitemap-jobs.xml") ?: return emptyList()
        val urls = Regex("<loc>(https://[^<]+/job/[^<]+)</loc>").findAll(xml)
            .map { it.groupValues[1] }.toList()

        val matches = urls.mapNotNull { url ->
            val slug = url.substringAfterLast("/job/")
            val slugText = slug.replace(Regex("^\\d+-"), "").replace("-", " ")
            if (Profile.titleMatches(slugText, query)) url to slugText else null
        }

        val jobs = mutableListOf<Job>()
        for ((url, slugText) in matches.take(15)) { // cap page fetches
            var title = slugText.split(" ").joinToString(" ") { it.replaceFirstChar { c -> c.uppercase() } }
            var company = "Unknown"
            var jobLocation = "See posting"
            val page = Http.get(url)
            if (page != null) {
                // niceboard og:title format: "Title at Company"
                Regex("<meta property=\"og:title\" content=\"([^\"]+)\"").find(page)?.let { m ->
                    val idx = m.groupValues[1].lastIndexOf(" at ")
                    if (idx > 0) {
                        title = m.groupValues[1].substring(0, idx).trim()
                        company = m.groupValues[1].substring(idx + 4).trim()
                    } else {
                        title = m.groupValues[1].trim()
                    }
                }
                Regex("\"jobLocation\"[^}]*\"address[^}]*\"name\"\\s*:\\s*\"([^\"]+)\"")
                    .find(page)?.let { jobLocation = it.groupValues[1] }
            }
            val job = Job.make(
                title = title,
                company = company,
                url = url,
                location = jobLocation,
                source = "jobsincarbon",
            )
            if (Profile.matchesLocations(job)) jobs.add(job)
        }
        return jobs
    }

    // --- Remote Impact (HTML, climate & environment category) ---------------------------

    fun remoteimpact(query: String): List<Job> {
        val jobs = mutableListOf<Job>()
        for (page in 1..3) {
            val suffix = if (page == 1) "" else "page/$page/"
            val html = Http.get("https://remoteimpact.org/jobs/category/climate-environment/$suffix")
                ?: break
            val cards = Regex(
                "<a href=\"(/jobs/[^\"]+)\"[^>]*>([^<]+)</a>\\s*</h2>\\s*<p[^>]*>([^<]*)</p>"
            ).findAll(html).toList()
            if (cards.isEmpty()) break
            for (m in cards) {
                val (path, title, company) = m.destructured
                val job = Job.make(
                    title = title.trim(),
                    company = company.trim().ifBlank { "Unknown" },
                    url = "https://remoteimpact.org$path",
                    location = "Remote",
                    wfhPolicy = "Remote",
                    source = "remoteimpact",
                )
                if (Profile.titleMatches(job.title, query)) jobs.add(job)
            }
        }
        return jobs
    }

    // --- CDRjobs (carbon dioxide removal board; GraphQL behind a Wix pym iframe) -------------
    // Public, auth-free Apollo API; the clientKey is the embed UUID. `limit` caps at 100 and
    // introspection is off, so the ~600-job catalog is walked with the keyset cursor
    // (takeAfter = last publishedAt, takeAfterId = last id). Mirrors search_cdrjobs in agent.py.

    private const val CDR_GRAPHQL = "https://prod-backend.cdrjobs.earth/graphql"
    private const val CDR_CLIENT_KEY = "f2c03c34-d670-448c-b5ca-77b6b69b6f26"
    private const val CDR_MAX_PAGES = 15
    private const val CDR_QUERY =
        "query searchJobs(\$clientKey: String!, \$filters: jobFiltersInput!, " +
            "\$pagination: paginationInput!) { searchJobs(clientKey: \$clientKey, " +
            "filters: \$filters, pagination: \$pagination) { data { id title sourceUrl " +
            "locations { country city } remote currency minSalary maxSalary publishedAt " +
            "company { name } } } }"
    private val CDR_COUNTRIES = mapOf(
        "ch" to "Switzerland", "de" to "Germany", "nl" to "Netherlands", "at" to "Austria",
        "fr" to "France", "gb" to "United Kingdom", "ie" to "Ireland", "es" to "Spain",
        "it" to "Italy", "be" to "Belgium", "dk" to "Denmark", "se" to "Sweden",
        "no" to "Norway", "fi" to "Finland", "pt" to "Portugal", "pl" to "Poland",
        "us" to "United States", "ca" to "Canada", "au" to "Australia", "in" to "India",
        "is" to "Iceland", "ae" to "United Arab Emirates",
    )

    fun cdrjobs(query: String): List<Job> {
        val headers = mapOf("Origin" to "https://embed.cdrjobs.earth")
        val jobs = mutableListOf<Job>()
        var pagination = JSONObject().put("limit", 100)
        for (page in 1..CDR_MAX_PAGES) {
            val reqBody = JSONObject()
                .put("query", CDR_QUERY)
                .put(
                    "variables",
                    JSONObject()
                        .put("clientKey", CDR_CLIENT_KEY)
                        .put("filters", JSONObject())
                        .put("pagination", pagination),
                )
                .toString()
            val body = Http.postJson(CDR_GRAPHQL, reqBody, headers) ?: break
            val data = try {
                JSONObject(body).optJSONObject("data")?.optJSONObject("searchJobs")
                    ?.optJSONArray("data") ?: break
            } catch (e: Exception) {
                break
            }
            for (i in 0 until data.length()) {
                val j = data.getJSONObject(i)
                val title = j.optString("title").trim()
                if (title.isEmpty() || !Profile.titleMatches(title, query)) continue
                val locs = j.optJSONArray("locations") ?: JSONArray()
                val places = (0 until locs.length()).map { k ->
                    val l = locs.getJSONObject(k)
                    val code = l.optString("country").lowercase()
                    listOf(
                        l.optString("city").takeIf { it.isNotBlank() && it != "null" },
                        CDR_COUNTRIES[code] ?: code.uppercase(),
                    ).filterNotNull().filter { it.isNotBlank() }.joinToString(", ")
                }
                val remote = j.optString("remote") // "yes" | "hybrid" | "no"
                var location = places.joinToString("; ")
                if (remote == "yes") {
                    location = if (location.isEmpty()) "Remote" else "Remote — $location"
                }
                val lo = j.optDouble("minSalary", 0.0).toLong()
                val hi = j.optDouble("maxSalary", 0.0).toLong()
                val currency = j.optString("currency").takeIf { it != "null" }.orEmpty()
                val salary = when {
                    lo > 0 && hi > 0 -> "%,d–%,d $currency".format(lo, hi).trim()
                    lo > 0 || hi > 0 -> "%,d $currency".format(maxOf(lo, hi)).trim()
                    else -> ""
                }
                val job = Job.make(
                    title = title,
                    company = j.optJSONObject("company")?.optString("name").orEmpty(),
                    url = j.optString("sourceUrl"),
                    location = location.ifEmpty { "See posting" },
                    salary = salary,
                    postedDate = j.optString("publishedAt").take(10),
                    wfhPolicy = when (remote) {
                        "yes" -> "Remote"; "hybrid" -> "Hybrid"; "no" -> "On-site"; else -> "Unknown"
                    },
                    source = "cdrjobs",
                )
                if (Profile.matchesLocations(job)) jobs.add(job)
            }
            if (data.length() < 100) break
            val last = data.getJSONObject(data.length() - 1)
            pagination = JSONObject().put("limit", 100)
                .put("takeAfter", last.optString("publishedAt"))
                .put("takeAfterId", last.optString("id"))
        }
        return jobs
    }
}
