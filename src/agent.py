import json
import Classy
from groq import Groq
import subprocess
import shlex
import src.scraper as scraper
from datetime import datetime
from json_repair import repair_json
import src.alexa_integration as alexa
import src.smart_tv as tv
import os





BASE_DIR = os.path.dirname(os.path.abspath(__file__))

html_dir=BASE_DIR+'/../Flask/templates/agent/'
static_dir=BASE_DIR+'/../Flask/static/'


from dotenv import load_dotenv

load_dotenv()


custom_domain = os.getenv("CUSTOM_DOMAIN")

if custom_domain is None:
    import socket
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)

    try:
        # Doesn't actually send data
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
    finally:
        s.close()

    url='http://'+ip+':6767'
else:
    url=custom_domain





location=None
client = None

agent_messages=[]
tools=[]


def reset(groq_key, location_innit,tts=False):
    global client
    client = Groq(api_key=groq_key)
    global location
    location=location_innit



    global agent_messages
    global tools
    with open(static_dir+"context.md", "r", encoding="utf-8") as f:
        content = f.read()
    if tts:
        agent_messages=[
            {
            "role": "system",
            "content": f"You are a helpful AI agent. Use the tools only if you need them to get data. Your output will be via text-to-speech, so format accordingly. Today's date is {datetime.now().strftime('%B %d, %Y')}."
            },
            {
            "role": "system",
            "content": f"Context about the user is stored in context.md. Here are the contents of that file: {content}"
            }
        ]
    else:
        agent_messages=[
            {
            "role": "system",
            "content": f"You are a helpful AI agent. Use the tools only if you need them to get data. Today's date is {datetime.now().strftime('%B %d, %Y')}."
            },
            {
            "role": "system",
            "content": f"Context about the user is stored in context.md. Here are the contents of that file: {content}"
            }
        ]

    best_sites = [
        {
            "weather": ["localconditions.com"],
            "news": ["bbc.com", "atoztimes.com"]
        }
    ]
    
    tools = [
        {
            "type": "function",
            "function": {
                "name": "save_context",
                "description": "Save important information about the user for future interactions. This information is stored in context.md, and should be referred to when relevant in future interactions. Use this tool to remember important details about the user, such as their preferences, important events in their life, and other relevant information that can help you better assist them in the future. Only use this tool when you have new information to add or need to update existing information. Do not use this tool excessively or for trivial details.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "contents": { "type": "string", "description": "Completely rewrites context.md" }
                    },
                    "required": ["url"]
                }
            }
        },
        {
            "type": "function",
            "function": {
                "name": "search",
                "description": "Fetches validated, up-to-date information for real-time queries. Only call this tool a maximum of 2 times per task — if you have sufficient data after 1-2 searches, proceed to the next step immediately. If you don't have the sufficient data, report back to the user after a maximium of 2 searches. Here is a website guide: "+str(best_sites),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": { "type": "string", "description": "The natural language query to answer" },
                        "site": { "type": ["string","null"], "description": "The site to be searched or None" }
                    },
                    "required": ["query"]
                }
            }
        },
        {
            "type": "function",
            "function": {
                "name": "read_web",
                "description": "Returns the first 3000 English characters of a webpage. Only use this if the user tells you to specifically look at a webpage.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "url": { "type": "string", "description": "URL of the webpage to read" }
                    },
                    "required": ["url"]
                }
            }
        },
        {
            "type": "function",
            "function": {
                "name": "create_page",
                "description": "Creates an HTML page for the user to see",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "filename": { "type": "string", "description": "name_of_your_webpage.html" },
                        "contents": { "type": "string", "description": "HTML code" }
                    },
                    "required": ["filename","contents"]
                }
            }
        },
        {
            "type": "function",
            "function": {
                "name": "create_file",
                "description": "Creates interoperable outputs for other applications and systems. Use for documents, data exports, scripts, configurations, automation artifacts, and other task-completing file outputs.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "filename": { "type": "string", "description": "name_of_your_file.something" },
                        "contents": { "type": "string", "description": "Contents of file, can leave blank" }
                    },
                    "required": ["filename","contents"]
                }
            }
        },
        {
            "type": "function",
            "function": {
                "name": "alexa",
                "description": "Sends directions to an Alexa connected to my house. Use this command for common Alexa funcitons, such as smarthome tasks and music.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "command": { "type": "string", "description": "Directions for Alexa" }
                    },
                    "required": ["command"]
                }
            }
        },
        {
            "type": "function",
            "function": {
                "name": "tv_control",
                "description": "Sends directions to a smart TV",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "command": { "type": "string", "description": "Either on, off, volume up, or volume down" }
                    },
                    "required": ["command"]
                }
            }
        },
        {
            "type": "function",
            "function": {
                "name": "youtube",
                "description": "Plays youtube videos on my smart TV.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "media_id": { "type": "string", "description": "The exact media id of the video to be played." }
                    },
                    "required": ["media_id"]
                }
            }
        },
        {
            "type": "function",
            "function": {
                "name": "run_bash_command",
                "description": "Allows you to run commands directly on this machine. If a user asks you to do something, immediately create a command and run it. Don't run multiple commands without reporting back to the user.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "command": { "type": "string", "description": "BASH Command" }
                    },
                    "required": ["command"]
                }
            }
        }
    ]   



def agent(user_input=None, system_input=None,tool_input=None,tool_id=None,tool_name=None):
    global agent_messages
    global scrape
    global tags
    global messages
    global reset
    global tools
    model="openai/gpt-oss-120b"
    check_tools=tools
    if user_input and system_input:
        raise Exception("You cannot have both user input and system input at the same time.")
    elif user_input:

        if user_input.lower() == 'reset':
            reset()
            return "Agent reset."


        intent, certainty = Classy.classify(user_input,location)
        print(intent)
        tag=intent[0]
        



        agent_messages.append(
            {
            "role": "user",
            "content": user_input
        }
        )
        agent_input=user_input

#####################################################################################################################################
        print(tag)
        if tag == 'Greeting/goodbye':
            eco_messages=[agent_messages[0], agent_messages[-1]]
            model="llama-3.1-8b-instant"
            check_tools=None
        elif tag == 'Personal-question' or  tag == 'Banter' or tag == 'About-user':
            if len(agent_messages) > 2:
                eco_messages=[agent_messages[0]] + agent_messages[-3:]
            else:
                eco_messages=agent_messages
            model="llama-3.1-8b-instant"
            check_tools=None
        elif tag == 'Search':
            if len(agent_messages) > 2:
                eco_messages=[agent_messages[0]] + agent_messages[-3:]
            else:
                eco_messages=agent_messages


        elif tag == 'Context' or tag == 'Edit':
            if len(agent_messages) > 6:
                eco_messages=[agent_messages[0]] + agent_messages[-7:]
            else:
                eco_messages=agent_messages


        elif tag == 'Coding' or tag == 'Writing' or tag == 'List' or tag == 'Suggest':
            if len(agent_messages) > 4:
                eco_messages=[agent_messages[0]] + agent_messages[-5:]
            else:
                eco_messages=agent_messages


        elif tag == 'Logic' or tag == 'Math' or tag == 'Explain':
            if len(agent_messages) > 4:
                eco_messages=[agent_messages[0]] + agent_messages[-5:]
            else:
                eco_messages=agent_messages
        elif tag == 'Utility':
            if len(agent_messages) > 4:
                eco_messages=[agent_messages[0]] + agent_messages[-5:]
            else:
                eco_messages=agent_messages
        else:
            if len(agent_messages) > 4:
                eco_messages=[agent_messages[0]] + agent_messages[-5:]
            else:
                eco_messages=agent_messages

######################################################################################################################################
    elif system_input:
        agent_messages.append(
            {
            "role": "system",
            "content": system_input
        }
        )
        agent_input=system_input
        eco_messages=agent_messages
    elif tool_input:
        agent_messages.append(
            {
            "role": "tool",
            "tool_call_id": tool_id,
            "name": tool_name,
            "content": tool_input
        }
        )
        agent_input=tool_input
        # Find all user message indices
        user_indices = [i for i, m in enumerate(agent_messages) if m['role'] == 'user']
        # Start from 2 user messages ago, or the first user message if there aren't 2
        start_index = user_indices[-2] if len(user_indices) >= 2 else user_indices[0]
        eco_messages = [agent_messages[0]] + agent_messages[start_index:]
    else:
        raise Exception("You must have either user input or system input.")
    '''
    print('##########################################################################')
    print('\n')
    print(eco_messages)
    print('\n')
    print('##########################################################################')
    '''
    print('Reveived: '+agent_input)
    completion = client.chat.completions.create(
        model=model,
        messages=eco_messages,
        temperature=1,
        tools=check_tools,
        top_p=1,
        stream=False,
        stop=None
    )
    assistant_msg=completion.choices[0].message
    buffer=completion.choices[0].message.content
    print('Agent: '+buffer if buffer is not None else ' ')









    if assistant_msg.tool_calls:

        agent_messages.append(
            {
            "role": "assistant",
            "tool_calls": [
                {
                    "id": tc.id,
                    "type": tc.type,
                    "function": {
                        "name": tc.function.name,
                        "arguments": tc.function.arguments
                    }
                }
                for tc in assistant_msg.tool_calls
            ]
            }
        )
    

        
        tool_call = assistant_msg.tool_calls[0]
        command_name = tool_call.function.name
        tool_args = tool_call.function.arguments  # JSON string

        fixed_tool_args = repair_json(tool_args)

        args_dict = json.loads(fixed_tool_args)
        parameter = args_dict.get('query') or args_dict.get('site') or args_dict.get('url') or args_dict.get('command') or args_dict.get('filename') or args_dict.get('contents') or args_dict.get('media_id') or None
        print('Agent called tool: '+command_name)
        print('Agent parameter: '+parameter if parameter else ' ')
        if command_name == 'search':


            query=args_dict.get('query')

            site=args_dict.get('site') or None
            print('Site: '+site if site else None)
            if site is not None:
                web_data=scraper.get_result(parameter+' - '+site)
            else:
                web_data=scraper.get_result(parameter)
            #print(web_data)



            s_messages=[{"role": "system", "content": "Query: "+parameter+".The following data has been scraped from a website, and your job is to clean up and structure the following data, answering the query. Only include information closely related to the query. Respond in full sentences."+web_data[0]}]


            stream = client.chat.completions.create(
                model="llama-3.3-70b-versatile",
                messages=s_messages,
                stream=True
            )
            report=''
            for chunk in stream:
                report+=chunk.choices[0].delta.content or ""


            
            return agent(tool_input=report+" - "+web_data[1], tool_id=tool_call.id,tool_name=command_name)

        elif command_name == 'read_web':
            site_data=scraper.scrape(parameter)
            return agent(tool_input=site_data, tool_id=tool_call.id,tool_name=command_name)
        elif command_name == 'save_context':
            contents=args_dict.get('contents')
            with open(static_dir+"context.md", "w", encoding="utf-8") as f:
                f.write(contents)
            return agent(tool_input="Context saved.", tool_id=tool_call.id,tool_name=command_name)

        elif command_name == 'create_file':

            filename=args_dict.get('filename')
            if "/" in filename or "\\" in filename:
                return agent(tool_input="Invalid filename.", tool_id=tool_call.id,tool_name=command_name)
            contents=args_dict.get('contents')
            with open(static_dir+filename, "w", encoding="utf-8") as f:
                f.write(contents)
            return agent(tool_input="Your file is accessable at "+url+"/static/"+filename, tool_id=tool_call.id,tool_name=command_name)
        



        elif command_name == 'create_page':

            filename=args_dict.get('filename')
            if "/" in filename or "\\" in filename:
                return agent(tool_input="Invalid filename.", tool_id=tool_call.id,tool_name=command_name)
            contents=args_dict.get('contents')
            with open(html_dir+filename, "w", encoding="utf-8") as f:
                f.write(contents)
            return agent(tool_input="Your site is live at "+url+"/agent/agent/"+filename.replace('.html',''), tool_id=tool_call.id,tool_name=command_name)
        
        elif command_name == 'alexa':
            alexa.send_to_alexa(parameter)
            output='Command sent to alexa.'
            return agent(tool_input=output, tool_id=tool_call.id,tool_name=command_name)
        
        elif command_name == 'tv_control':
            if parameter.lower() == 'on':
                tv.tv_on()
                output='TV turned on.'
            elif parameter.lower() == 'off':
                tv.tv_off()
                output='TV turned off.'
            elif parameter.lower() == 'volume up':
                tv.volume_up()
                output='TV volume turned up.'
            elif parameter.lower() == 'volume down':
                tv.volume_down()
                output='TV volume turned down.'
            return agent(tool_input=output, tool_id=tool_call.id,tool_name=command_name)
        elif command_name == 'youtube':
            media_id=args_dict.get('media_id')
            tv.play_youtube(media_id)
            output='Playing video on TV.'
            return agent(tool_input=output, tool_id=tool_call.id,tool_name=command_name)
        elif command_name == 'run_bash_command':
            print(parameter)
            run_command='y'
            if run_command.lower() == 'y':
                proc = subprocess.Popen(
                    f'{parameter}',
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    shell=True
                )
                stdout, stderr = proc.communicate()
                print(stdout,stderr)
                output=(stdout + "\n" + stderr).strip()
            else:
                output='User denied command'
            if output is None or output == '':
                output='Command was run successfully, Report back to the user.'
            print(output)
            return agent(tool_input=output, tool_id=tool_call.id,tool_name=command_name)
    elif buffer is not None:
        agent_messages.append(
            {
            "role": "assistant",
            "content": buffer
            }
        )
        print('\n')
    print(agent_messages)
    print('\n')
    return buffer


'''
while True:
    output=agent(user_input=input(': '))
    print(output)
    '''
