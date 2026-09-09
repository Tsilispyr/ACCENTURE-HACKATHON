import requests

url = "http://localhost:8010"

incident = {
    "Incident ID": "INC-1042",
    "Service": "payment-service",
    "Severity": "Unknown",
    "Description": "Customers report payment failures for approximately 15 minutes.",
    "Error": "Database connection timeout."
}
response = requests.get(url )
response.raise_for_status()
print(response.json())

# get call to the /incidents endpoint
response = requests.get(url+ "/chat",
    params={"message": "2*2"})

response.raise_for_status()
print(response.json())



# post call to the /incidents endpoint
response = requests.post(
    url + "/incidents",
    json=incident
)

response.raise_for_status()

result = response.json()

print(result)



