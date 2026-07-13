import urllib.request
for i in range(2000, 2050):
    try:
        req = urllib.request.Request(f'https://es.soccerway.com/teams/spain/real-club-celta-de-vigo/{i}/', headers={'User-Agent': 'Mozilla/5.0'})
        r = urllib.request.urlopen(req)
        html = r.read().decode('utf-8')
        if 'Celta' in html.split('<title>')[1].split('</title>')[0]:
            print("Found Celta! ID:", i)
            break
    except:
        pass
