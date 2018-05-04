from pyspark.sql import Row
import random
from math import exp
from settings import *
from utils import initSpark, rdd2tsv, analyze_url, same_domains


institutions = pd.read_csv(institutionsFile, sep='\t')
institutions['URL'] = institutions['URL'].apply(lambda u: re.sub(r'^(www[0-9]?\.)|(web\.)', r'', u))
repositories = pd.read_csv(repositoriesFile)
repositories['URL'] = repositories['URL'].apply(lambda u: re.sub(r'^http://(www\.)?', r'', u))
blacklistURLs = open(blacklistURLsFile).read().splitlines()

project_url = 'http://sci-lens.org'
graph_nodes = {'tweetWithoutURL':project_url+'#tweetWithoutURL', 'HTTPError':project_url+'#HTTPError', 'TimeoutError':project_url+'#TimeoutError', 'institution':project_url+'#institution', 'repository':project_url+'#repository', 'source':project_url+'#source'}
sources = institutions['URL'].tolist() + repositories['URL'].tolist()

#Resolve url
def resolveURL(url):
    if url=='':
        return graph_nodes['tweetWithoutURL']
        
    try:
        #Follow the redirections of a URL
        r = requests.head(url, allow_redirects='HEAD', timeout=urlTimout)
        if r.status_code != 403:            
            r.raise_for_status()

        #Avoid blacklisted and flat URLs
        domain, path = analyze_url(r.url)
        if domain in blacklistURLs or path in ['', '/']:
            r.url = ''

        return r.url

    #Catch the different errors       
    except requests.HTTPError as e:
        return graph_nodes['HTTPError']
    except:
        return graph_nodes['TimeoutError']

#Get outgoing links from article
def get_out_links(url, epoch_decay, last_pass):
    
    #custom urls for special nodes
    if url.startswith(project_url):
        return ['']

    domain, path = analyze_url(url)

    #predefined sources
    for s in sources:
        if s in domain:
            return [s]

    #other sources
    if any(suffix in domain for suffix in ['.edu', '.ac.uk', '.gov']):
        if path in ['', '/']:
            return[graph_nodes['repository']]
        else:
            return[domain]

    #Do not expand links over the last pass
    if last_pass:
        return ['']

    try:
        headers = {"User-Agent":"Mozilla/5.0 (X11; U; Linux i686) Gecko/20071127 Firefox/2.0.0.11"}
        r = requests.get(url, allow_redirects='HEAD', timeout=urlTimout, headers=headers)
        soup = BeautifulSoup(r.content, 'html.parser')
    except:
        return ['']

    #get all links except for self and blacklisted links
    links = []
    source_links = []
    for link in soup.findAll('a'):
        link = link.get('href') or ''
        link_domain, link_path = analyze_url(link)
        if not same_domains(domain, link_domain) and link_domain not in blacklistURLs and link_path not in ['', '/']:
            links.append(link)
            for s in sources:
                if s in link_domain:
                    source_links.append(link)

    #if there are links to the predefined sources, return only them
    if source_links:
        return list(set(source_links))    

    #otherwise return with probability 1/k*epoch_decay the k outgoing links
    pruned_links = []
    if len(links) != 0:
        link_prob = (1/len(links)) * epoch_decay
    for link in links:
        if random.random() < link_prob:
            pruned_links.append(link)
    return list(set(pruned_links))

#Create the nth level of the diffusion graph
def graph_epoch_n(frontier, epoch, last_pass):

    spark = initSpark()

    if epoch == 0:
        urlRegex = 'http[s]?://(?:[a-zA-Z]|[0-9]|[$-_@.&+]|[!*\(\),]|(?:%[0-9a-fA-F][0-9a-fA-F]))+'
        documents = spark.sparkContext.textFile(twitterCorpusFile, minPartitions=(conf['cores']-1)) 
        documents = documents.map(lambda r: (lambda l=r.split('\t'): Row(source_url=l[0], tweet=l[1], timestamp=datetime.strptime(l[2], '%Y-%m-%d %H:%M:%S'), popularity=int(l[3]), RTs=int(l[4]), user_country=l[5]))())
        documents = documents.flatMap(lambda r: [Row(source_url=r.source_url, timestamp=r.timestamp, popularity=r.popularity, RTs=r.RTs, user_country=r.user_country, target_url=resolveURL(u)) for u in re.findall(urlRegex, r.tweet) or ['']])
        documents = documents.map(lambda r : '\t'.join(str(a) for a in [r.source_url, r.timestamp, r.popularity, r.RTs, r.user_country, r.target_url]))
        rdd2tsv(documents, diffusion_graph_dir+'epoch_'+str(epoch)+'.tsv', ['source_url','timestamp', 'popularity', 'RTs', 'user_country', 'target_url'])
    else:
        documents = spark.sparkContext.parallelize(frontier, numSlices=(conf['cores']-1))
        documents = documents.flatMap(lambda r: [Row(source_url=r, target_url=l) for l in get_out_links(r, epoch_decay=exp(-epoch), last_pass=last_pass) or ['']])
        documents = documents.map(lambda r : '\t'.join(str(a) for a in [r.source_url, r.target_url]))
        rdd2tsv(documents, diffusion_graph_dir+'epoch_'+str(epoch)+'.tsv', ['source_url', 'target_url'])


#Create diffusion graph
def create_graph():

    #initialize graph
    G=nx.DiGraph()

    for v in institutions['URL'].tolist():
        G.add_edge(v, graph_nodes['institution'])

    for v in repositories['URL'].tolist():
        G.add_edge(v, graph_nodes['repository'])

    G.add_edge(graph_nodes['institution'], graph_nodes['source'])
    G.add_edge(graph_nodes['repository'], graph_nodes['source'])

    epoch = 0
    frontier = []
    connected_components = 0
    last_pass = False
    while True:

        #Expand graph
        if not useCache or not os.path.exists(diffusion_graph_dir+'epoch_'+str(epoch)+'.tsv'):
            graph_epoch_n(frontier, epoch, last_pass)

        df = pd.read_csv(diffusion_graph_dir+'epoch_'+str(epoch)+'.tsv', sep='\t').dropna()
        G =  nx.compose(G, nx.from_pandas_edgelist(df, source='source_url', target='target_url', create_using=nx.DiGraph()))
        frontier = [x for x in G.nodes() if G.out_degree(x) == 0]

        print('Epoch:', epoch)
        print('Connected Components:', nx.number_connected_components(G.to_undirected()))
        print('Frontier Size:', len(frontier))

        
        if last_pass:
            break
        
        #last pass condition
        if epoch != 0 and (connected_components - nx.number_connected_components(G.to_undirected())) / connected_components < components_ratio:
            last_pass = True
        connected_components = nx.number_connected_components(G.to_undirected())
        epoch +=1
    

    #print(G.in_degree(graph_nodes['tweetWithoutURL']))
    print(G.in_degree('nih.gov'))
    print(G.out_degree(graph_nodes['institution']))

    G = sorted(G.in_degree, key=lambda x: x[1], reverse=True)    
    print(G[:20])