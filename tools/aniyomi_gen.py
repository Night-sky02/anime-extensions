#!/usr/bin/env python3
"""
aniyomi_gen.py - Génère le squelette complet d'une extension Aniyomi à partir de l'URL d'un site.

    python aniyomi_gen.py https://exemple.tld --name "Exemple" --lang fr --out ./out

Le script télécharge quelques pages (accueil, une fiche anime, une page d'épisode),
devine les sélecteurs CSS par heuristiques, puis écrit le projet (Gradle + Kotlin + icône)
et un REPORT.md qui liste ce qui a été détecté (et avec quelle confiance).

Limites : sites rendus en JavaScript, protégés par Cloudflare, ou lecteurs vidéo
obfusqués -> il faudra compléter à la main les parties marquées TODO.
Dépendance : beautifulsoup4 (pip install beautifulsoup4)
"""
import argparse
import re
import struct
import sys
import urllib.request
import zlib
from collections import Counter
from pathlib import Path
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

UA = "Mozilla/5.0 (Linux; Android 13) AppleWebKit/537.36 Chrome/120 Mobile Safari/537.36"
REPORT = []


def note(msg):
    REPORT.append(msg)
    print(msg)


# ----------------------------------------------------------------------------- réseau
def fetch(url):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept-Language": "fr,en;q=0.8"})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            html = r.read().decode(r.headers.get_content_charset() or "utf-8", "replace")
    except Exception as e:  # noqa
        note(f"- ⚠️ Impossible de charger {url} : {e}")
        return None
    return BeautifulSoup(html, "html.parser")


# ----------------------------------------------------------------------------- sélecteurs
def sig(el):
    classes = el.get("class") or []
    return el.name + "".join("." + c for c in classes)


def css_for_group(els):
    """Sélecteur CSS commun à un groupe d'éléments (tag + classes partagées par >=80 %)."""
    n = len(els)
    cnt = Counter(c for e in els for c in set(e.get("class") or []))
    common = [c for c, k in cnt.items() if k >= 0.8 * n]
    base = els[0].name + "".join("." + c for c in sorted(common))
    parent = els[0].parent
    if parent is not None and parent.name not in ("body", "html", "[document]"):
        if parent.get("id"):
            return f"#{parent['id']} > {base}"
        pcls = parent.get("class") or []
        if pcls:
            return f"{parent.name}.{pcls[0]} > {base}"
    return base


def best_group(anchors, min_size=3, max_level=3):
    """Parmi des <a>, trouve l'ancêtre répété (la 'carte') le plus fréquent."""
    for level in range(max_level + 1):
        groups = {}
        for a in anchors:
            el = a
            for _ in range(level):
                el = el.parent
                if el is None:
                    break
            if el is None or el.name in ("body", "html"):
                continue
            groups.setdefault(sig(el) + "|" + (el.parent.name if el.parent else ""), []).append(el)
        if not groups:
            continue
        key = max(groups, key=lambda k: len(groups[k]))
        g = groups[key]
        # dédoublonne
        uniq = list({id(e): e for e in g}.values())
        if len(uniq) >= max(min_size, int(0.6 * len(anchors))):
            return uniq
    return None


def pick_img_attr(img):
    for attr in ("data-src", "data-lazy-src", "data-original", "data-cfsrc", "src"):
        if img.get(attr) and not str(img.get(attr)).startswith("data:"):
            return attr
    return "src"


def find_listing(soup, base):
    """Détecte la grille d'anime (lien + image répétés)."""
    anchors = [a for a in soup.select("a[href]") if a.find("img")]
    group = best_group(anchors, min_size=4)
    if not group:
        return None
    sel = css_for_group(group)
    items = soup.select(sel) or group
    first = items[0]
    a = first if first.name == "a" else first.select_one("a[href]")
    img = first.find("img")
    # titre
    title = ("attr", "title", None)
    tsel = first.select_one('[class*="title" i], [class*="name" i], [class*="tt" i], h2, h3, h4')
    if tsel is not None and tsel.get_text(strip=True):
        title = ("text", css_rel(tsel, first), None)
    elif a is not None and a.get("title"):
        title = ("attr", "title", None)
    elif img is not None and img.get("alt"):
        title = ("img-alt", "alt", None)
    return {
        "selector": sel,
        "count": len(items),
        "link_on_self": first.name == "a",
        "title": title,
        "img_attr": pick_img_attr(img) if img else "src",
        "sample_href": urljoin(base, a["href"]) if a is not None else None,
    }


def css_rel(el, root):
    """Sélecteur simple relatif à root."""
    cls = el.get("class") or []
    return el.name + ("." + cls[0] if cls else "")


PAGE_RE = re.compile(r"(.*?)(/page/|[?&]page=|/page-|[?&]paged=|[?&]p=)(\d+)(.*)")


def find_pagination(soup, base, url):
    nxt = None
    for sel in ('a[rel="next"]', "a.next", "li.next a", ".pagination .next a", "a.next.page-numbers",
                ".nextpostslink", "a.page-link[rel=next]"):
        if soup.select_one(sel):
            nxt = sel
            break
    if not nxt:
        for a in soup.select("a[href]"):
            if re.fullmatch(r"\s*(next|suivant|suiv\.?|»|›|>|→)\s*", a.get_text(" ", strip=True), re.I):
                cls = a.get("class") or []
                nxt = "a." + cls[0] if cls else None
                if not nxt and a.parent is not None and a.parent.get("class"):
                    nxt = f"{a.parent.name}.{a.parent['class'][0]} > a"
                break
    template = None
    for a in soup.select("a[href]"):
        m = PAGE_RE.match(urljoin(base, a["href"]))
        if m and m.group(3) in ("2", "3"):
            template = m.group(1) + m.group(2) + "{page}" + m.group(4)
            break
    return nxt, template


def find_nav_link(soup, base, words):
    best = None
    for a in soup.select("nav a[href], header a[href], .menu a[href], ul.nav a[href], a[href]"):
        text = a.get_text(" ", strip=True).lower()
        if text and len(text) < 30 and any(w in text for w in words):
            href = urljoin(base, a["href"])
            if urlparse(href).netloc == urlparse(base).netloc and not href.endswith("#"):
                best = href
                break
    return best


def find_search(soup, base):
    for form in soup.select("form"):
        for inp in form.select("input[name]"):
            if inp.get("type") in ("search", "text", None) and inp["name"].lower() in (
                    "s", "q", "search", "keyword", "query", "keywords", "k", "title"):
                action = urljoin(base, form.get("action") or "/")
                return {"url": action, "param": inp["name"]}
    return None


# ----------------------------------------------------------------------------- détails / épisodes / vidéo
def analyze_details(soup, base):
    d = {}
    h = soup.select_one("h1")
    if h:
        d["title"] = css_rel(h, soup)
    for sel in ('[class*="synopsis" i]', '[class*="description" i]', '[class*="summary" i]',
                '[class*="plot" i]', '[itemprop="description"]', ".entry-content p"):
        el = soup.select_one(sel)
        if el and len(el.get_text(strip=True)) > 40:
            d["description"] = sel
            break
    gen = soup.select('a[href*="genre"], a[href*="categor"], a[rel="tag"]')
    if len(gen) >= 2:
        g0 = gen[0]
        d["genre"] = ('a[href*="genre"]' if "genre" in g0["href"]
                      else 'a[href*="categor"]' if "categor" in g0["href"] else 'a[rel="tag"]')
    img = soup.select_one('[class*="poster" i] img, [class*="thumb" i] img, [class*="cover" i] img, '
                          'meta[property="og:image"]')
    if img is not None:
        if img.name == "meta":
            d["thumb"] = ("meta", 'meta[property="og:image"]', "content")
        else:
            d["thumb"] = ("img", css_rel(img.parent, soup) + " img" if img.parent.get("class") else "img",
                          pick_img_attr(img))
    ep_anchors = [a for a in soup.select("a[href]")
                  if re.search(r"(épisode|episode|[-_/ ]ep[-_ ]?|eps?)[-_ ]?\d+", a["href"] + " " + a.get_text(), re.I)]
    grp = best_group(ep_anchors, min_size=2, max_level=2) if ep_anchors else None
    if grp:
        d["episodes"] = css_for_group(grp)
        d["episodes_count"] = len(grp)
        d["ep_sample"] = urljoin(base, (grp[0] if grp[0].name == "a" else grp[0].select_one("a"))["href"])
        d["ep_on_a"] = grp[0].name == "a"
        nums = [int(m.group(1)) for e in grp for m in [re.search(r"(\d+)\D*$", e.get_text(" ", strip=True))] if m]
        d["ep_ascending"] = len(nums) >= 2 and nums[0] < nums[-1]
    return d


MEDIA_RE = re.compile(r"https?:[^\"'\s<>]+?\.(?:m3u8|mp4)[^\"'\s<>]*", re.I)


def analyze_video(soup, base):
    info = {"iframes": [], "direct": []}
    for f in soup.select("iframe[src], iframe[data-src]"):
        info["iframes"].append(urljoin(base, f.get("src") or f.get("data-src")))
    info["direct"] = MEDIA_RE.findall(str(soup))[:3]
    return info


# ----------------------------------------------------------------------------- génération
def png(size, rgb):
    raw = b"".join(b"\x00" + bytes(rgb) * size for _ in range(size))

    def chunk(t, d):
        c = struct.pack(">I", len(d)) + t + d
        return c + struct.pack(">I", zlib.crc32(t + d) & 0xFFFFFFFF)

    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))


def kq(s):
    """Échappe pour une chaîne Kotlin."""
    return s.replace("\\", "\\\\").replace('"', '\\"').replace("$", "\\$")


KOTLIN = r'''package eu.kanade.tachiyomi.animeextension.@@LANG@@.@@PKG@@

import eu.kanade.tachiyomi.animesource.model.AnimeFilterList
import eu.kanade.tachiyomi.animesource.model.AnimesPage
import eu.kanade.tachiyomi.animesource.model.Hoster
import eu.kanade.tachiyomi.animesource.model.SAnime
import eu.kanade.tachiyomi.animesource.model.SEpisode
import eu.kanade.tachiyomi.animesource.model.Video
import eu.kanade.tachiyomi.animesource.online.AnimeHttpSource
import eu.kanade.tachiyomi.network.GET
import keiyoushi.annotation.Source
import keiyoushi.network.get
import keiyoushi.utils.useAsJsoup
import okhttp3.Headers
import okhttp3.HttpUrl.Companion.toHttpUrl
import okhttp3.Request
import okhttp3.Response
import org.jsoup.nodes.Document
import org.jsoup.nodes.Element

// Généré par aniyomi_gen.py - vérifie les sélecteurs et les TODO (voir REPORT.md).
// name / lang / baseUrl viennent du bloc source {} de build.gradle.kts (KSP) : ne pas les déclarer ici.
@Source
abstract class @@CLASS@@ : AnimeHttpSource() {

    override val supportsLatest = true

    override fun headersBuilder(): Headers.Builder = super.headersBuilder()
        .add("Referer", "$baseUrl/")

    // ============================== Liste ==============================

    private fun pagedUrl(first: String, template: String?, page: Int): String =
        if (page <= 1 || template == null) first else template.replace("{page}", page.toString())

    private fun Element.imageUrl(): String {
        val img = selectFirst("img") ?: return ""
        return img.absUrl("@@IMG_ATTR@@").ifEmpty { img.absUrl("src") }
    }

    private fun animeFromElement(element: Element) = SAnime.create().apply {
@@FROM_ELEMENT@@
    }

    private fun parseAnimePage(document: Document): AnimesPage {
        val animes = document.select("@@LIST_SEL@@").map(::animeFromElement)
        val hasNextPage = @@HAS_NEXT@@
        return AnimesPage(animes, hasNextPage)
    }

    // ============================== Populaires ==============================

    override fun popularAnimeRequest(page: Int): Request =
        GET(pagedUrl(@@POP_URL@@, @@POP_TPL@@, page), headers)

    override fun popularAnimeParse(response: Response): AnimesPage = parseAnimePage(response.useAsJsoup())

    // ============================== Récents ==============================

    override fun latestUpdatesRequest(page: Int): Request =
        GET(pagedUrl(@@LAT_URL@@, @@LAT_TPL@@, page), headers)

    override fun latestUpdatesParse(response: Response): AnimesPage = parseAnimePage(response.useAsJsoup())

    // ============================== Recherche ==============================

    override fun searchAnimeRequest(page: Int, query: String, filters: AnimeFilterList): Request {
@@SEARCH_REQUEST@@
    }

    override fun searchAnimeParse(response: Response): AnimesPage = parseAnimePage(response.useAsJsoup())

    // ============================== Détails ==============================

    override fun animeDetailsParse(response: Response): SAnime {
        val document = response.useAsJsoup()
        return SAnime.create().apply {
@@DETAILS@@
        }
    }

    // ============================== Épisodes ==============================

    // La liste doit être triée du plus récent au plus ancien (ordre de la source).
    override fun episodeListParse(response: Response): List<SEpisode> {
        val document = response.useAsJsoup()
        return document.select("@@EP_SEL@@").map { element ->
            SEpisode.create().apply {
                val a = if (element.tagName() == "a") element else element.selectFirst("a")!!
                setUrlWithoutDomain(a.absUrl("href"))
                name = element.text().ifEmpty { a.attr("title") }
                episode_number = EPISODE_NUMBER_REGEX.findAll(name).lastOrNull()
                    ?.value?.toFloatOrNull() ?: -1F
            }
        }@@EP_REVERSE@@
    }

    // Pas de saisons sur ce site.
    override fun seasonListParse(response: Response): List<SAnime> = throw UnsupportedOperationException()

    // ============================== Hébergeurs / Vidéos ==============================

    override fun hosterListRequest(episode: SEpisode): Request = GET(baseUrl + episode.url, headers)

    override fun hosterListParse(response: Response): List<Hoster> {
        val document = response.useAsJsoup()
        val hosters = mutableListOf<Hoster>()

        document.select("iframe[src], iframe[data-src]").forEach { frame ->
            val src = frame.absUrl("src").ifEmpty { frame.absUrl("data-src") }
            if (src.isNotEmpty()) {
                hosters.add(Hoster(hosterUrl = src, hosterName = src.toHttpUrl().host, internalData = src))
            }
        }
        // Liens .m3u8 / .mp4 directement présents dans le HTML
        MEDIA_REGEX.findAll(document.html().replace("\\/", "/")).map { it.value }.distinct().forEach { url ->
            hosters.add(Hoster(hosterUrl = url, hosterName = "Direct", internalData = url))
        }
        return hosters
    }

    override suspend fun getVideoList(hoster: Hoster): List<Video> {
        val url = hoster.internalData
        if (MEDIA_REGEX.matches(url)) {
            return listOf(Video(videoUrl = url, videoTitle = hoster.hosterName, headers = headers))
        }
@@EXTRACTORS@@
        // Générique : cherche un flux .m3u8 / .mp4 dans la page de l'iframe.
        val body = client.get(url, headers.newBuilder().set("Referer", "$baseUrl/").build())
            .use { it.body.string() }
        return MEDIA_REGEX.findAll(body.replace("\\/", "/")).map { it.value }.distinct()
            .map { Video(videoUrl = it, videoTitle = hoster.hosterName, headers = headers) }
            .toList()
    }

    companion object {
        private val EPISODE_NUMBER_REGEX = Regex("""\d+(?:\.\d+)?""")
        private val MEDIA_REGEX = Regex("""https?:[^"'\s<>\\]+?\.(?:m3u8|mp4)[^"'\s<>\\]*""")
    }
}
'''

EXTRACTOR_LIBS = {
    "filemoon": "filemoonextractor", "mp4upload": "mp4uploadextractor", "dood": "doodextractor",
    "mixdrop": "mixdropextractor", "ok.ru": "okruextractor", "kwik": "kwikextractor",
    "lulu": "luluextractor", "dailymotion": "dailymotionextractor", "sibnet": "sibnetextractor",
    "streamtape": "streamtapeextractor", "streamwish": "streamwishextractor", "voe": "voeextractor",
    "vidhide": "vidhideextractor", "vidmoly": "vidmolyextractor", "uqload": "uqloadextractor",
    "sendvid": "sendvidextractor", "vk.com": "vkextractor", "streamlare": "streamlareextractor",
    "vudeo": "vudeoextractor", "upstream": "upstreamextractor", "streamhub": "streamhubextractor",
    "vidguard": "vidguardextractor", "pixeldrain": "pixeldrainextractor", "streamsilk": "streamsilkextractor",
}


def suggest_extractors(iframes):
    found = {}
    for u in iframes:
        host = urlparse(u).netloc.lower()
        for key, lib in EXTRACTOR_LIBS.items():
            if key in host:
                found[lib] = host
    return found


def build_kotlin(cfg):
    L = cfg["listing"]
    lines = []
    link = "element" if L["link_on_self"] else 'element.selectFirst("a[href]")!!'
    lines.append(f"        setUrlWithoutDomain({link}.absUrl(\"href\"))")
    kind, val, _ = L["title"]
    if kind == "text":
        lines.append(f'        title = element.selectFirst("{kq(val)}")?.text().orEmpty()')
    elif kind == "img-alt":
        lines.append('        title = element.selectFirst("img")?.attr("alt").orEmpty()')
    else:
        lines.append(f'        title = {link}.attr("title").ifEmpty {{ element.text() }}')
    lines.append("        thumbnail_url = element.imageUrl()")

    s = cfg.get("search")
    if s:
        path = urlparse(s["url"]).path or "/"
        sr = (f'        val url = "$baseUrl{kq(path)}".toHttpUrl().newBuilder()\n'
              f'            .addQueryParameter("{kq(s["param"])}", query)\n'
              f'        // TODO: pagination de la recherche à vérifier sur le site\n'
              f'        if (page > 1) url.addQueryParameter("page", page.toString())\n'
              f'        return GET(url.build(), headers)')
    else:
        sr = ('        // TODO: aucun formulaire de recherche détecté.\n'
              '        return GET("$baseUrl/?s=$query", headers)')

    D = cfg["details"]
    dl = [f'            title = document.selectFirst("{kq(D.get("title", "h1"))}")?.text().orEmpty()']
    if "thumb" in D:
        k, sel, attr = D["thumb"]
        dl.append(f'            thumbnail_url = document.selectFirst("{kq(sel)}")?.absUrl("{attr}")' if k == "img"
                  else f'            thumbnail_url = document.selectFirst("{kq(sel)}")?.attr("content")')
    if "description" in D:
        dl.append(f'            description = document.selectFirst("{kq(D["description"])}")?.text()')
    else:
        dl.append('            // TODO: synopsis non détecté')
    if "genre" in D:
        dl.append(f'            genre = document.select("{kq(D["genre"])}").joinToString {{ it.text() }}')
    dl.append("            // TODO: statut (en cours / terminé) à parser si le site l'indique")
    dl.append('            status = SAnime.UNKNOWN')

    def rel(u):
        p = urlparse(u)
        path = p.path + (("?" + p.query) if p.query else "")
        return '"$baseUrl' + kq(path or "/") + '"' if p.netloc == urlparse(cfg["base"]).netloc else f'"{kq(u)}"'

    def tpl(t):
        return rel(t) if t else "null"

    pop, lat = cfg["pop"], cfg["lat"]
    ext_lines = ""
    for lib, host in cfg.get("extractors", {}).items():
        ext_lines += f"        // TODO: {host} -> utiliser :lib:{lib} (voir son API dans lib/{lib})\n"
    nxt = cfg["next"]
    has_next = f'document.selectFirst("{kq(nxt)}") != null' if nxt else "false // TODO: pagination non détectée"
    rev = ".reversed()" if D.get("ep_ascending") else ""
    return (KOTLIN
            .replace("@@LANG@@", cfg["lang"]).replace("@@PKG@@", cfg["pkg"]).replace("@@CLASS@@", cfg["cls"])
            .replace("@@IMG_ATTR@@", L["img_attr"])
            .replace("@@POP_URL@@", rel(pop["url"])).replace("@@POP_TPL@@", tpl(pop["tpl"]))
            .replace("@@LAT_URL@@", rel(lat["url"])).replace("@@LAT_TPL@@", tpl(lat["tpl"]))
            .replace("@@LIST_SEL@@", kq(L["selector"])).replace("@@HAS_NEXT@@", has_next)
            .replace("@@FROM_ELEMENT@@", "\n".join(lines))
            .replace("@@SEARCH_REQUEST@@", sr).replace("@@DETAILS@@", "\n".join(dl))
            .replace("@@EP_SEL@@", kq(D.get("episodes", "TODO-episode-selector")))
            .replace("@@EP_REVERSE@@", rev).replace("@@EXTRACTORS@@", ext_lines))


GRADLE = '''import io.github.keiyoushi.gradle.api.ContentWarning

plugins {
    alias(kei.plugins.extension)
}

yuzono {
    name = "@@NAME@@"
    versionCode = 1
    contentWarning = ContentWarning.@@CW@@
    libVersion = "16"

    source {
        lang = "@@LANG@@"
        baseUrl = "@@BASE@@"
    }
}
@@DEPS@@'''


def main():
    ap = argparse.ArgumentParser(description="Génère une extension Aniyomi depuis l'URL d'un site")
    ap.add_argument("url")
    ap.add_argument("--name", help="Nom affiché (défaut: domaine)")
    ap.add_argument("--lang", default="fr")
    ap.add_argument("--out", default="./out")
    ap.add_argument("--repo", help="Chemin d'un clone de yuzono/anime-extensions : copie directement le module dedans")
    ap.add_argument("--nsfw", action="store_true", help="ContentWarning.NSFW au lieu de SAFE")
    a = ap.parse_args()

    base = a.url.rstrip("/")
    host = urlparse(base).netloc.replace("www.", "")
    name = a.name or host.split(".")[0].capitalize()
    pkg = re.sub(r"[^a-z0-9]", "", name.lower()) or "source"
    cls = re.sub(r"[^A-Za-z0-9]", "", name.title()) or "Source"
    a.lang = a.lang.split("-")[0].lower()  # le dépôt veut le code de langue majeur (pt et non pt-BR)

    note(f"# Rapport de génération - {name} ({base})\n")
    home = fetch(base)
    if home is None:
        sys.exit("Impossible de charger le site (Cloudflare / réseau ?). Voir le message ci-dessus.")

    # --- pages de liste
    def listing_for(words, label):
        url = find_nav_link(home, base, words) or base
        soup = home if url == base else fetch(url)
        info = find_listing(soup, url) if soup else None
        if not info and url != base:
            url, soup = base, home
            info = find_listing(home, base)
        nxt, tpl = find_pagination(soup, url, url) if soup else (None, None)
        note(f"- **{label}** : {url} -> sélecteur `{info['selector'] if info else '???'}`"
             f" ({info['count'] if info else 0} éléments), pagination `{nxt}` / `{tpl}`")
        return url, info, nxt, tpl

    pop_url, pop_info, nxt, pop_tpl = listing_for(
        ["popul", "top", "trending", "catalogue", "liste", "anime", "tous", "all"], "Populaires")
    lat_url, lat_info, nxt2, lat_tpl = listing_for(
        ["récent", "recent", "latest", "nouveau", "dernier", "new", "épisodes", "episodes"], "Récents")
    listing = pop_info or lat_info
    if not listing:
        sys.exit("Aucune grille d'anime détectée (site probablement rendu en JavaScript).")
    nxt = nxt or nxt2

    search = find_search(home, base)
    note(f"- **Recherche** : {search or 'non détectée (TODO)'}")

    # --- fiche anime
    details, vinfo = {}, {"iframes": [], "direct": []}
    if listing.get("sample_href"):
        dsoup = fetch(listing["sample_href"])
        if dsoup:
            details = analyze_details(dsoup, listing["sample_href"])
            note(f"- **Fiche anime** ({listing['sample_href']}) : {details}")
            if details.get("ep_sample"):
                esoup = fetch(details["ep_sample"])
                if esoup:
                    vinfo = analyze_video(esoup, details["ep_sample"])
                    note(f"- **Page épisode** ({details['ep_sample']}) : iframes={vinfo['iframes']}, "
                         f"liens média directs={vinfo['direct']}")
    if "episodes" not in details:
        note("- ⚠️ Liste d'épisodes non détectée : remplace `TODO-episode-selector` à la main.")
    if not vinfo["iframes"] and not vinfo["direct"]:
        note("- ⚠️ Aucune source vidéo trouvée dans le HTML statique : le lecteur est probablement chargé "
             "en JS/AJAX. `videoListParse` devra être adapté (onglet Réseau des DevTools).")

    exts = suggest_extractors(vinfo["iframes"])
    if exts:
        note("- **Extracteurs existants dans lib/** pour les iframes trouvées : "
             + ", ".join(f"`:lib:{l}` ({h})" for l, h in exts.items()))
    cfg = dict(base=base, name=name, lang=a.lang, pkg=pkg, cls=cls, listing=listing, details=details,
               search=search, next=nxt, pop=dict(url=pop_url, tpl=pop_tpl), lat=dict(url=lat_url, tpl=lat_tpl),
               extractors=exts)

    out = Path(a.repo) / "src" / a.lang / pkg if a.repo else Path(a.out) / "src" / a.lang / pkg
    ktdir = out / "src" / "eu" / "kanade" / "tachiyomi" / "animeextension" / a.lang / pkg
    ktdir.mkdir(parents=True, exist_ok=True)
    (ktdir / f"{cls}.kt").write_text(build_kotlin(cfg), encoding="utf-8")
    deps = "".join(f'    implementation(project(":lib:{l}"))\n' for l in exts)
    deps = f"\ndependencies {{\n{deps}}}\n" if deps else ""
    (out / "build.gradle.kts").write_text(
        GRADLE.replace("@@NAME@@", kq(name)).replace("@@LANG@@", a.lang).replace("@@BASE@@", base)
        .replace("@@CW@@", "NSFW" if a.nsfw else "SAFE").replace("@@DEPS@@", deps), encoding="utf-8")
    h = sum(map(ord, name))
    rgb = (60 + h * 7 % 150, 60 + h * 13 % 150, 60 + h * 29 % 150)
    for d, size in (("mdpi", 48), ("hdpi", 72), ("xhdpi", 96), ("xxhdpi", 144), ("xxxhdpi", 192)):
        p = out / "res" / f"mipmap-{d}"
        p.mkdir(parents=True, exist_ok=True)
        (p / "ic_launcher.png").write_bytes(png(size, rgb))
    mod = f":src:{a.lang}:{pkg}"
    note("\n## À faire ensuite\n"
         + ("1. Le module est déjà copié dans le dépôt (`--repo`).\n" if a.repo else
            "1. Copie `src/" + a.lang + "/" + pkg + "` dans `src/` d'un clone de yuzono/anime-extensions.\n")
         + "2. Cherche les `TODO` dans le fichier `.kt`.\n"
         "3. Remplace les icônes générées (carrés unis) par un vrai logo (carré aux coins arrondis, voir CONTRIBUTING.md).\n"
         f"4. Vérifie la compilation : `./gradlew {mod}:compileDebugKotlin` puis `./gradlew {mod}:spotlessApply`.\n"
         f"5. APK installable : `./gradlew {mod}:assembleDebug` ; teste sur le vrai site avant toute PR.\n"
         "6. Extension basée sur la lib 16 : il faut une version de l'app qui la supporte.")
    Path(a.out).mkdir(parents=True, exist_ok=True)
    (Path(a.out) / "REPORT.md").write_text("\n".join(REPORT) + "\n", encoding="utf-8")
    print(f"\n✅ Projet écrit dans {out}\n📄 Rapport : {Path(a.out) / 'REPORT.md'}")


if __name__ == "__main__":
    main()
