
## We are doing three different data sources in this project:
1. Fabric
2. Databricks
3. Snowflake

## Authentication Ideas:
### fabric
- We have SSO for fabric that is via Azure Entra ID, which should show all the workspaces, warehouse, everything. The user should be able to select the warehouse and semantic model
- All the further tool calls should be limited to that warehouse and workspace all those gaurdrails.
### Databricks
- Same as what already exists in the code
### Snowflake
- Same like Fabric
- Same as what already exists.

## MCP Idea - Customizability
All the connectors when connected to the claude, should be uniquely identified based on who is connecting, and should what all they are connecting, and they should be able to configure the connection details even after the connector connection is established.

The flow is like:
1. User goes to claude custom connector, and presses connect
2. Claude redirects to our MCP's authentication page, which asks them to login via SSO
3. It shows options for databricks, fabric and snowflake
4. It asks them to name the connection, custom prompt.
5. It stores these details, which should be later modifiable.
6. The connection details should be encryped with some kind of thing, may be the something in the claude's bearer token that depends on the user's sso, which we can crack, and should be encryped using this while storing in the db for security purpose. .(I might be wrong on this idea too, cause I am not sure how far my idea is useful, but we need to encrypt the details before storing right)
7. The claude gets the tools, and can call. 
8. All platforms should have same functions, and all these functions will be switched dynamically while toolcalling based on the user's configurations done on the initial setup.
9. All these configurations should be customizable and editable by the user on a different UI that we will build on this database, so that the user does not have to edit the details each time by disconnecting and reconnecting.

So, further my thoughts are like:
User gets in SSO to our MCP
User SSO - His different connections to claude with each having its own configurations
THe above can be edited by the UI.

All platforms should provide a list of same funcitons, so the names and parameters are same, but the process hapening inside is dependent on the platform.

What do you think? Should we have a seperate MCP server for each platform and redirect based on like what already exists? Or should be use one, that just switches the credentials and the platform functions dynamically based on what the user has asked.

How do we handle the sessions? Authentications? 1000 users using it at a time? One user wanting to use multiple data platforms at a time?

Data Governence, Security and Compliances.
How to restrict AI in read and write access, how to make it not see whats in the other workspaces?

Lets talk about this and build a fresh architecture. 




Lets make it extendable further. So, when the MCP is added, can the claude pop up the custom page that can be used to modify the details for the connector?
In this place they configure their connection, and here they have to signin with the SSO.