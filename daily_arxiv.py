import os
import re
import json
import html
import time
import arxiv
import yaml
import logging
import argparse
import datetime
import requests

logging.basicConfig(format='[%(asctime)s %(levelname)s] %(message)s',
                    datefmt='%m/%d/%Y %H:%M:%S',
                    level=logging.INFO)

arxiv_url = "http://arxiv.org/"

# PapersWithCode's API is gone (paperswithcode.com now redirects to Hugging
# Face's paper listing), so code links are found on a best-effort basis via
# a github.com URL in the abstract, or a GitHub code search fallback.
GITHUB_CODE_SEARCH_URL = "https://api.github.com/search/code"
GITHUB_URL_RE = re.compile(r'https?://github\.com/[\w.-]+/[\w.-]+')

# free, no-auth-required citation lookup - used as a rough quality signal
SEMANTIC_SCHOLAR_URL = "https://api.semanticscholar.org/graph/v1/paper/arXiv:{}"

def load_config(config_file:str) -> dict:
    '''
    config_file: input config file path
    return: a dict of configuration
    '''
    # make filters pretty
    def pretty_filters(**config) -> dict:
        keywords = dict()
        EXCAPE = '\"'
        QUOTA = '' # NO-USE
        OR = ' OR ' # TODO
        def parse_filters(filters:list):
            ret = ''
            for idx in range(0,len(filters)):
                filter = filters[idx]
                if len(filter.split()) > 1:
                    ret += (EXCAPE + filter + EXCAPE)
                else:
                    ret += (QUOTA + filter + QUOTA)
                if idx != len(filters) - 1:
                    ret += OR
            return ret
        for k,v in config['keywords'].items():
            keywords[k] = parse_filters(v['filters'])
        return keywords
    with open(config_file,'r') as f:
        config = yaml.load(f,Loader=yaml.FullLoader)
        config['kv'] = pretty_filters(**config)
        logging.info(f'config = {config}')
    return config

def get_authors(authors, first_author = False):
    output = str()
    if first_author == False:
        output = ", ".join(str(author) for author in authors)
    else:
        output = str(authors[0])
    return output

def sort_papers(papers):
    output = dict()
    keys = list(papers.keys())
    keys.sort(reverse=True)
    for key in keys:
        output[key] = papers[key]
    return output

def pretty_math(s:str) -> str:
    ret = ''
    match = re.search(r"\$.*\$", s)
    if match == None:
        return s
    math_start,math_end = match.span()
    space_trail = space_leading = ''
    if s[:math_start][-1] != ' ' and '*' != s[:math_start][-1]: space_trail = ' '
    if s[math_end:][0] != ' ' and '*' != s[math_end:][0]: space_leading = ' '
    ret += s[:math_start]
    ret += f'{space_trail}${match.group()[1:-1].strip()}${space_leading}'
    ret += s[math_end:]
    return ret

def escape_table_cell(s:str) -> str:
    # a literal "|" would otherwise break the markdown table it's embedded in
    return s.replace('|', '\\|')

def fetch_semantic_scholar_info(arxiv_id:str):
    '''
    Rough, free signals via Semantic Scholar (no API key required): citation
    count (quality proxy) and author affiliations (used to spot papers from
    well-known labs - see is_top_lab_paper). Returns None on any
    failure/not-yet-indexed paper - callers should treat that as "unknown",
    not "zero"/"none".
    '''
    try:
        r = requests.get(SEMANTIC_SCHOLAR_URL.format(arxiv_id),
                          params={"fields": "citationCount,authors.affiliations"}, timeout=10)
    except requests.RequestException as e:
        logging.warning(f'Semantic Scholar lookup failed for {arxiv_id}: {e}')
        return None
    finally:
        time.sleep(1)  # be polite to the shared, unauthenticated rate limit

    if r.status_code != 200:
        logging.warning(f'Semantic Scholar lookup returned {r.status_code} for {arxiv_id}')
        return None

    result = r.json()
    affiliations = []
    for author in result.get('authors') or []:
        affiliations.extend(author.get('affiliations') or [])

    return {
        "citations": result.get('citationCount'),
        "affiliations": sorted(set(affiliations)),
    }

def is_top_lab_paper(paper:dict, top_labs) -> bool:
    '''
    Best-effort match of a paper's (Semantic Scholar-sourced) author
    affiliations against a configurable list of well-known lab/institution
    names. Coverage is limited - Semantic Scholar's affiliation data is
    often sparse or missing, especially for very recent papers.
    '''
    if not top_labs:
        return False
    haystack = ' '.join(paper.get('affiliations') or []).lower()
    if not haystack:
        return False
    return any(lab.lower() in haystack for lab in top_labs)

def sort_papers_with_priority(papers:dict, top_labs):
    '''
    Same newest-first ordering as sort_papers, but papers matching
    top_labs are pinned to the front of the list (still newest-first
    among themselves).
    '''
    ordered = sort_papers(papers)
    top, rest = {}, {}
    for paper_id, paper in ordered.items():
        if is_top_lab_paper(paper, top_labs):
            top[paper_id] = paper
        else:
            rest[paper_id] = paper
    return {**top, **rest}

def extract_code_link_from_abstract(abstract:str):
    '''
    Many arxiv authors link their own code straight in the abstract - this is
    a free, zero-API-call way to pick that up.
    '''
    match = GITHUB_URL_RE.search(abstract or '')
    if not match:
        return None
    return match.group(0).rstrip('.,)')

def find_code_link(paper:dict):
    '''
    Best-effort code link lookup for a single paper dict (needs 'abstract'
    and 'id'). Tries the abstract first (cheap), then falls back to a GitHub
    code search for the arxiv id inside repo READMEs (requires GITHUB_TOKEN -
    GitHub's code search API rejects unauthenticated requests).
    '''
    code = extract_code_link_from_abstract(paper.get('abstract', ''))
    if code:
        return code

    token = os.environ.get('GITHUB_TOKEN')
    if not token:
        return None

    arxiv_id = paper['id']
    headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "Authorization": f"Bearer {token}",
    }
    params = {"q": f'"{arxiv_id}" in:readme'}
    try:
        r = requests.get(GITHUB_CODE_SEARCH_URL, headers=headers, params=params, timeout=10)
    except requests.RequestException as e:
        logging.warning(f'GitHub code search failed for {arxiv_id}: {e}')
        return None
    finally:
        time.sleep(2)  # stay well under GitHub's ~30 req/min authenticated search limit

    if r.status_code != 200:
        logging.warning(f'GitHub code search returned {r.status_code} for {arxiv_id}')
        return None

    results = r.json()
    if results.get('total_count', 0) > 0:
        return results['items'][0]['repository']['html_url']
    return None

def get_daily_papers(topic,query="slam", max_results=2):
    """
    @param topic: str
    @param query: str
    @return data: dict, {topic: {paper_id: paper_dict}}
    """
    content = dict()
    search_engine = arxiv.Search(
        query = query,
        max_results = max_results,
        sort_by = arxiv.SortCriterion.SubmittedDate
    )
    client = arxiv.Client()

    max_retries = 5
    last_error = None
    for attempt in range(max_retries):
        try:
            results = list(client.results(search_engine))
            break
        except arxiv.HTTPError as e:
            status = getattr(e, 'status', None)
            if status is None:
                status = getattr(e, 'status_code', None)
            if status is not None:
                try:
                    status = int(status)
                except (TypeError, ValueError):
                    status = None
            if status is None:
                is_retriable = re.search(
                    r'\bHTTP(?:/[0-9.]+)?(?: Error)?\s+(429|500|502|503|504)\b',
                    str(e),
                ) is not None
            else:
                is_retriable = status in {429, 500, 502, 503, 504}
            if is_retriable and attempt < max_retries - 1:
                wait_time = 60 * (attempt + 1)
                logging.warning(f"Transient arXiv error ({e}), waiting {wait_time}s before retry {attempt + 1}/{max_retries}")
                time.sleep(wait_time)
                last_error = e
                client = arxiv.Client()
            else:
                raise
    else:
        raise last_error

    for result in results:

        paper_id            = result.get_short_id()
        paper_title         = result.title
        paper_abstract      = result.summary.replace("\n"," ")
        paper_authors       = get_authors(result.authors)
        paper_first_author  = get_authors(result.authors,first_author = True)
        update_time         = result.updated.date()

        logging.info(f"Time = {update_time} title = {paper_title} author = {paper_first_author}")

        # eg: 2108.09112v1 -> 2108.09112
        ver_pos = paper_id.find('v')
        if ver_pos == -1:
            paper_key = paper_id
        else:
            paper_key = paper_id[0:ver_pos]
        paper_url = arxiv_url + 'abs/' + paper_key

        content[paper_key] = {
            "date": str(update_time),
            "title": paper_title,
            "first_author": paper_first_author,
            "authors": paper_authors,
            "id": paper_key,
            "url": paper_url,
            "abstract": paper_abstract,
            "code": extract_code_link_from_abstract(paper_abstract),
            "citations": None,
            "affiliations": [],
        }

    return {topic:content}

def update_paper_links(filename):
    '''
    weekly job: backfill code links and citation counts for existing papers
    that don't have them yet (repos are often published, and citations start
    accruing, well after the paper itself first appears).
    '''
    with open(filename,"r") as f:
        content = f.read()
        json_data = json.loads(content) if content else {}

    for keyword, papers in json_data.items():
        logging.info(f'keywords = {keyword}')
        for paper_id, paper in papers.items():
            if not paper.get('code'):
                code = find_code_link(paper)
                if code:
                    paper['code'] = code
                    logging.info(f'paper_id = {paper_id}, found code link = {code}')

            if paper.get('citations') is None:
                info = fetch_semantic_scholar_info(paper['id'])
                if info is not None:
                    paper['citations'] = info['citations']
                    paper['affiliations'] = info['affiliations']
                    logging.info(f"paper_id = {paper_id}, citations = {info['citations']}, "
                                 f"affiliations = {info['affiliations']}")

    with open(filename,"w") as f:
        json.dump(json_data,f)

def update_json_file(filename,data_dict):
    '''
    daily update json file using data_dict
    '''
    with open(filename,"r") as f:
        content = f.read()
        if not content:
            m = {}
        else:
            m = json.loads(content)

    json_data = m.copy()

    # update papers in each keywords
    for data in data_dict:
        for keyword in data.keys():
            papers = data[keyword]

            if keyword in json_data.keys():
                json_data[keyword].update(papers)
            else:
                json_data[keyword] = papers

    with open(filename,"w") as f:
        json.dump(json_data,f)

def write_badges(f):
    # we don't like long string, break it!
    f.write((f"[contributors-shield]: https://img.shields.io/github/"
             f"contributors/Gaming722/cv-arxiv-daily.svg?style=for-the-badge\n"))
    f.write((f"[contributors-url]: https://github.com/Gaming722/"
             f"cv-arxiv-daily/graphs/contributors\n"))
    f.write((f"[forks-shield]: https://img.shields.io/github/forks/Gaming722/"
             f"cv-arxiv-daily.svg?style=for-the-badge\n"))
    f.write((f"[forks-url]: https://github.com/Gaming722/"
             f"cv-arxiv-daily/network/members\n"))
    f.write((f"[stars-shield]: https://img.shields.io/github/stars/Gaming722/"
             f"cv-arxiv-daily.svg?style=for-the-badge\n"))
    f.write((f"[stars-url]: https://github.com/Gaming722/"
             f"cv-arxiv-daily/stargazers\n"))
    f.write((f"[issues-shield]: https://img.shields.io/github/issues/Gaming722/"
             f"cv-arxiv-daily.svg?style=for-the-badge\n"))
    f.write((f"[issues-url]: https://github.com/Gaming722/"
             f"cv-arxiv-daily/issues\n\n"))

def render_readme_md(filename, md_filename, show_badge=True, top_labs=None):
    """
    @param filename: str, source json data
    @param md_filename: str, target README.md
    """
    DateNow = str(datetime.date.today()).replace('-', '.')

    with open(filename,"r") as f:
        content = f.read()
        data = json.loads(content) if content else {}

    # clean md file if already exist else create it
    with open(md_filename,"w+") as f:
        pass

    with open(md_filename,"a+") as f:
        f.write("## Updated on " + DateNow + "\n")
        f.write("> Usage instructions: [here](https://github.com/Gaming722/cv-arxiv-daily/blob/main/docs/README.md#usage)\n\n")

        # table of contents
        f.write("<details>\n")
        f.write("  <summary>Table of Contents</summary>\n")
        f.write("  <ol>\n")
        for keyword, day_content in data.items():
            if not day_content:
                continue
            kw = keyword.replace(' ','-')
            f.write(f"    <li><a href=#{kw.lower()}>{keyword}</a></li>\n")
        f.write("  </ol>\n")
        f.write("</details>\n\n")

        for keyword, day_content in data.items():
            if not day_content:
                continue
            f.write(f"## {keyword}\n\n")
            f.write("|Publish Date|Title|Authors|Citations|PDF|Code|\n"
                    + "|---|---|---|---|---|---|\n")

            day_content = sort_papers_with_priority(day_content, top_labs)
            for paper_id, paper in day_content.items():
                code_cell = f"**[link]({paper['code']})**" if paper.get('code') else "null"
                citations_cell = paper['citations'] if paper.get('citations') is not None else "-"
                title_cell = escape_table_cell(paper['title'])
                if is_top_lab_paper(paper, top_labs):
                    title_cell = "🏆 " + title_cell
                row = "|**{}**|**{}**|{} et.al.|{}|[{}]({})|{}|\n".format(
                    paper['date'], title_cell,
                    paper['first_author'], citations_cell, paper['id'], paper['url'], code_cell)
                f.write(pretty_math(row))

            f.write("\n")

            top_info = f"#Updated on {DateNow}"
            top_info = top_info.replace(' ','-').replace('.','')
            f.write(f"<p align=right>(<a href={top_info.lower()}>back to top</a>)</p>\n\n")

        if show_badge:
            write_badges(f)

    logging.info("Update Readme finished")

def render_wechat_md(filename, md_filename, show_badge=True):
    """
    @param filename: str, source json data
    @param md_filename: str, target wechat.md
    """
    DateNow = str(datetime.date.today()).replace('-', '.')

    with open(filename,"r") as f:
        content = f.read()
        data = json.loads(content) if content else {}

    with open(md_filename,"w+") as f:
        pass

    with open(md_filename,"a+") as f:
        f.write("> Updated on " + DateNow + "\n")
        f.write("> Usage instructions: [here](https://github.com/Gaming722/cv-arxiv-daily/blob/main/docs/README.md#usage)\n\n")

        for keyword, day_content in data.items():
            if not day_content:
                continue
            f.write(f"## {keyword}\n\n")

            day_content = sort_papers(day_content)
            for paper_id, paper in day_content.items():
                line = "- {}, **{}**, {} et.al., Paper: [{}]({})\n".format(
                    paper['date'], paper['title'], paper['first_author'],
                    paper['url'], paper['url'])
                f.write(pretty_math(line))

            f.write("\n")

        if show_badge:
            write_badges(f)

    logging.info("Update Wechat finished")

# Emitted once at the top of docs/index.md: lets a reader flip between the
# compact table (default) and a card grid that also shows each abstract.
GITPAGE_VIEW_TOGGLE_HTML = """<style>
.view-table { display: block; }
.view-cards { display: none; }
body.card-view .view-table { display: none; }
body.card-view .view-cards { display: grid; grid-template-columns: repeat(auto-fill, minmax(320px, 1fr)); gap: 1rem; }
.paper-card { border: 1px solid #ddd; border-radius: 12px; padding: 1rem; }
.paper-card h3 { margin: 0 0 0.5rem 0; font-size: 1.05rem; }
.paper-meta { color: #888; font-size: 0.85rem; margin: 0 0 0.5rem 0; }
.paper-abstract { font-size: 0.9rem; line-height: 1.4; margin: 0 0 0.5rem 0; }
.paper-links { margin: 0; font-size: 0.9rem; }
#view-toggle-btn { margin-bottom: 1rem; padding: 0.4rem 0.8rem; border-radius: 8px; cursor: pointer; }
</style>
<button id="view-toggle-btn">Switch to card view (with abstracts)</button>
<script>
(function () {
  var body = document.body;
  var btn = document.getElementById('view-toggle-btn');
  function applyView(view) {
    body.classList.toggle('card-view', view === 'cards');
    btn.textContent = view === 'cards'
      ? 'Switch to table view'
      : 'Switch to card view (with abstracts)';
  }
  applyView(localStorage.getItem('arxiv-view') || 'table');
  btn.addEventListener('click', function () {
    var next = body.classList.contains('card-view') ? 'table' : 'cards';
    localStorage.setItem('arxiv-view', next);
    applyView(next);
  });
})();
</script>

"""

def render_gitpage_md(filename, md_filename, show_badge=True, top_labs=None):
    """
    @param filename: str, source json data
    @param md_filename: str, target docs/index.md
    """
    DateNow = str(datetime.date.today()).replace('-', '.')

    with open(filename,"r") as f:
        content = f.read()
        data = json.loads(content) if content else {}

    with open(md_filename,"w+") as f:
        pass

    with open(md_filename,"a+") as f:
        f.write("---\n" + "layout: default\n" + "---\n\n")
        f.write(GITPAGE_VIEW_TOGGLE_HTML)

        f.write("## Updated on " + DateNow + "\n")
        f.write("> Usage instructions: [here](https://github.com/Gaming722/cv-arxiv-daily/blob/main/docs/README.md#usage)\n\n")

        for keyword, day_content in data.items():
            if not day_content:
                continue
            f.write(f"## {keyword}\n\n")

            day_content = sort_papers_with_priority(day_content, top_labs)

            # classic table view (default, no abstract)
            # markdown="1" tells kramdown to still parse the table syntax
            # inside this raw HTML block - without it, the pipe table is
            # left as literal text instead of being rendered.
            f.write('<div class="view-table" markdown="1">\n\n')
            f.write("| Publish Date | Title | Authors | Citations | PDF | Code |\n")
            f.write("|:---------|:-----------------------|:---------|:------|:------|:------|\n")
            for paper_id, paper in day_content.items():
                code_cell = f"**[link]({paper['code']})**" if paper.get('code') else "null"
                citations_cell = paper['citations'] if paper.get('citations') is not None else "-"
                title_cell = escape_table_cell(paper['title'])
                if is_top_lab_paper(paper, top_labs):
                    title_cell = "🏆 " + title_cell
                row = "|**{}**|**{}**|{} et.al.|{}|[{}]({})|{}|\n".format(
                    paper['date'], title_cell,
                    paper['first_author'], citations_cell, paper['id'], paper['url'], code_cell)
                f.write(pretty_math(row))
            f.write('\n</div>\n\n')

            # card view (with abstract)
            f.write('<div class="view-cards">\n')
            for paper_id, paper in day_content.items():
                title = html.escape(paper['title'])
                if is_top_lab_paper(paper, top_labs):
                    title = "🏆 " + title
                authors = html.escape(paper['authors'])
                abstract = html.escape(paper['abstract'])
                url = html.escape(paper['url'])
                citations_html = f' &middot; {paper["citations"]} citations' if paper.get('citations') is not None else ''
                code_html = ''
                if paper.get('code'):
                    code_html = f' &middot; <a href="{html.escape(paper["code"])}">Code</a>'
                f.write('<div class="paper-card">\n')
                f.write(f'<h3><a href="{url}">{title}</a></h3>\n')
                f.write(f'<p class="paper-meta">{paper["date"]} &middot; {authors}{citations_html}</p>\n')
                f.write(f'<p class="paper-abstract">{abstract}</p>\n')
                f.write(f'<p class="paper-links"><a href="{url}">PDF</a>{code_html}</p>\n')
                f.write('</div>\n')
            f.write('</div>\n\n')

        if show_badge:
            write_badges(f)

    logging.info("Update GitPage finished")

def demo(**config):
    data_collector = []

    keywords = config['kv']
    max_results = config['max_results']
    publish_readme = config['publish_readme']
    publish_gitpage = config['publish_gitpage']
    publish_wechat = config['publish_wechat']
    show_badge = config['show_badge']
    top_labs = config.get('top_labs', [])

    b_update = config['update_paper_links']
    logging.info(f'Update Paper Link = {b_update}')
    if config['update_paper_links'] == False:
        logging.info(f"GET daily papers begin")
        for topic, keyword in keywords.items():
            logging.info(f"Keyword: {topic}")
            try:
                data = get_daily_papers(topic, query = keyword,
                                                max_results = max_results)
            except arxiv.HTTPError as e:
                logging.warning(f"Skipping keyword {topic} after repeated arXiv failures: {e}")
                continue
            data_collector.append(data)
            print("\n")
        logging.info(f"GET daily papers end")

    # 1. update README.md file
    if publish_readme:
        json_file = config['json_readme_path']
        md_file   = config['md_readme_path']
        if config['update_paper_links']:
            update_paper_links(json_file)
        else:
            update_json_file(json_file,data_collector)
        render_readme_md(json_file, md_file, show_badge = show_badge, top_labs = top_labs)

    # 2. update docs/index.md file (to gitpage)
    if publish_gitpage:
        json_file = config['json_gitpage_path']
        md_file   = config['md_gitpage_path']
        if config['update_paper_links']:
            update_paper_links(json_file)
        else:
            update_json_file(json_file,data_collector)
        render_gitpage_md(json_file, md_file, show_badge = show_badge, top_labs = top_labs)

    # 3. Update docs/wechat.md file
    if publish_wechat:
        json_file = config['json_wechat_path']
        md_file   = config['md_wechat_path']
        if config['update_paper_links']:
            update_paper_links(json_file)
        else:
            update_json_file(json_file, data_collector)
        render_wechat_md(json_file, md_file, show_badge = show_badge)

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument('--config_path',type=str, default='config.yaml',
                            help='configuration file path')
    parser.add_argument('--update_paper_links', default=False,
                        action="store_true",help='whether to update paper links etc.')
    args = parser.parse_args()
    config = load_config(args.config_path)
    config = {**config, 'update_paper_links':args.update_paper_links}
    demo(**config)
