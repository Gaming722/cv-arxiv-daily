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

    for result in client.results(search_engine):

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
        }

    return {topic:content}

def update_paper_links(filename):
    '''
    weekly job: backfill code links for existing papers that don't have one
    yet (repos are often published well after the paper itself).
    '''
    with open(filename,"r") as f:
        content = f.read()
        json_data = json.loads(content) if content else {}

    for keyword, papers in json_data.items():
        logging.info(f'keywords = {keyword}')
        for paper_id, paper in papers.items():
            if paper.get('code'):
                continue
            code = find_code_link(paper)
            if code:
                paper['code'] = code
                logging.info(f'paper_id = {paper_id}, found code link = {code}')

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

def render_readme_md(filename, md_filename, show_badge=True):
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
        f.write("> Usage instructions: [here](./docs/README.md#usage)\n\n")

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
            f.write("|Publish Date|Title|Authors|PDF|Code|\n" + "|---|---|---|---|---|\n")

            day_content = sort_papers(day_content)
            for paper_id, paper in day_content.items():
                code_cell = f"**[link]({paper['code']})**" if paper.get('code') else "null"
                row = "|**{}**|**{}**|{} et.al.|[{}]({})|{}|\n".format(
                    paper['date'], paper['title'], paper['first_author'],
                    paper['id'], paper['url'], code_cell)
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
        f.write("> Usage instructions: [here](./docs/README.md#usage)\n\n")

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

def render_gitpage_md(filename, md_filename, show_badge=True):
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
        f.write("> Usage instructions: [here](./docs/README.md#usage)\n\n")

        for keyword, day_content in data.items():
            if not day_content:
                continue
            f.write(f"## {keyword}\n\n")

            day_content = sort_papers(day_content)

            # classic table view (default, no abstract)
            f.write('<div class="view-table">\n\n')
            f.write("| Publish Date | Title | Authors | PDF | Code |\n")
            f.write("|:---------|:-----------------------|:---------|:------|:------|\n")
            for paper_id, paper in day_content.items():
                code_cell = f"**[link]({paper['code']})**" if paper.get('code') else "null"
                row = "|**{}**|**{}**|{} et.al.|[{}]({})|{}|\n".format(
                    paper['date'], paper['title'], paper['first_author'],
                    paper['id'], paper['url'], code_cell)
                f.write(pretty_math(row))
            f.write('\n</div>\n\n')

            # card view (with abstract)
            f.write('<div class="view-cards">\n')
            for paper_id, paper in day_content.items():
                title = html.escape(paper['title'])
                authors = html.escape(paper['authors'])
                abstract = html.escape(paper['abstract'])
                url = html.escape(paper['url'])
                code_html = ''
                if paper.get('code'):
                    code_html = f' &middot; <a href="{html.escape(paper["code"])}">Code</a>'
                f.write('<div class="paper-card">\n')
                f.write(f'<h3><a href="{url}">{title}</a></h3>\n')
                f.write(f'<p class="paper-meta">{paper["date"]} &middot; {authors}</p>\n')
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

    b_update = config['update_paper_links']
    logging.info(f'Update Paper Link = {b_update}')
    if config['update_paper_links'] == False:
        logging.info(f"GET daily papers begin")
        for topic, keyword in keywords.items():
            logging.info(f"Keyword: {topic}")
            data = get_daily_papers(topic, query = keyword,
                                            max_results = max_results)
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
        render_readme_md(json_file, md_file, show_badge = show_badge)

    # 2. update docs/index.md file (to gitpage)
    if publish_gitpage:
        json_file = config['json_gitpage_path']
        md_file   = config['md_gitpage_path']
        if config['update_paper_links']:
            update_paper_links(json_file)
        else:
            update_json_file(json_file,data_collector)
        render_gitpage_md(json_file, md_file, show_badge = show_badge)

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
