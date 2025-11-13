from ollama import Client
client = Client(host='http://localhost:8008')
response = client.chat(model='taide', messages=[
  {
    'role': 'user',
    'content': '今天天氣如何',
  },
])