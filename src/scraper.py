from ddgs import DDGS

import requests
from bs4 import BeautifulSoup
import re


def scrape(url):    
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/115.0.0.0 Safari/537.36'
    }
    session = requests.Session()
    session.headers.update(headers)
    response = session.get(url, timeout=7)

    if response.status_code == 200:
        soup = BeautifulSoup(response.content, 'html.parser')
        text_elements = soup.find_all(['p'])
        page_text = ' '.join(element.get_text() for element in text_elements)
        page_text=page_text.replace('°', ' degrees ')
        page_text=page_text.replace('\u202f', ' ')
        page_text=page_text.replace('\u203a', ' ')
        page_text=page_text.replace(':00', " o'clock")
        page_text=page_text.replace('N F L', 'NFL')
        page_text=page_text.replace('M L B', 'MLB')
        page_text=page_text.replace('N B A', 'NBA')
        page_text=page_text.replace('N H L', 'NHL')
        page_text=page_text.replace('P M', 'pm')
        page_text=page_text.replace('A M', 'am')
        page_text=page_text.replace('\n', ' ')
        page_text=page_text.replace(' Q ', ' Quarter ')
        page_text=page_text.replace(' Final,', '')
        page_text=page_text.replace(' Sun,', ' Sunday,')
        page_text=page_text.replace(' Mon,', ' Monday,')
        page_text=page_text.replace(' Tue,', ' Tuesday,')
        page_text=page_text.replace(' Wed,', ' Wednesday,')
        page_text=page_text.replace(' Thu,', ' Thursday,')
        page_text=page_text.replace(' Fri,', ' Friday,')
        page_text=page_text.replace(' Sat,', ' Saturday,')
        
        page_text=page_text.replace(' Jan ', ' January ')
        page_text=page_text.replace(' Feb ', ' February ')
        page_text=page_text.replace(' Mar ', ' March ')
        page_text=page_text.replace(' Apr ', ' April ')
        page_text=page_text.replace(' Jun ', ' June ')
        page_text=page_text.replace(' Jul ', ' July ')
        page_text=page_text.replace(' Aug ', ' August ')
        page_text=page_text.replace(' Sep ', ' September ')
        page_text=page_text.replace(' Oct ', ' October ')
        page_text=page_text.replace(' Nov ', ' November ')
        page_text=page_text.replace(' Dec ', ' December ')
        return page_text
    else:
        return f"Error: Unable to retrieve content. Status code {response.status_code}"
def search(query):

    query = query.replace('"', '')

    results = DDGS().text(
        query,
        max_results=10
    )

    answer=[]

    for r in results:
        answer.append(r["href"])
    return answer
def get_result(query):
    if '--**retry search**--' in query:
        split_query=query.split('--**retry search**--')
        query=split_query[0]
        link=search(query, key)
    else:
        split_query=['--**retry search**--',11]
        link=search(query)
    ignore_list=['weather.com','finance.yahoo.com','www.mlb.com','www.espn.com','nytimes.com','www.accuweather.com']
    #print(link)
    for i in link:  
        data=scrape(i)
        if data is not None and ('https://' in i) and (data != '' and 'Error: Unable to retrieve content. Status code' not in data) and (i != int(split_query[1])) and (i.split('https://')[1].split('/')[0] not in ignore_list):
            return data[:3000], i, link.index(i)
