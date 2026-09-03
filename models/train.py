import json
from time import sleep
import nltk
from nltk.tokenize import word_tokenize
import random
import math

nltk.download("punkt_tab")

remove_punctuation = str.maketrans("", "", "!\"#&'(),.:;?@[\\]_`{|}~")

# Math
def activation(x):
    return 1 / (1 + math.exp(-x))

def activation_derivative(s):
    return 1

def softmax(logits):
    m = max(logits)
    exps = [math.exp(z - m) for z in logits]
    total = sum(exps)
    return [e / total for e in exps]


# Build dictionary

intents=json.loads(open('intents.json').read())["intents"]
tags=[]
for tag in intents:
    tags.append(tag["tag"])
patterns=[]
for tag in intents:
    tag_patterns=[]
    for pattern in tag["patterns"]:
        tag_patterns.append(pattern)
    patterns.append(tag_patterns) 

dictionary=[]

for tag in tags:
    tag_index=tags.index(tag)
    for pattern in patterns[tag_index]:
        tokens=word_tokenize(pattern.lower().translate(remove_punctuation))
        for token in tokens:
            if token not in dictionary:
                dictionary.append(token)

# Hyperparams
input_size=len(dictionary)
hidden_size=32
output_size=len(tags)
learning_rate=0.10
epochs=60
print("Input size: ", input_size)
print("Hidden size: ", hidden_size)
print("Output size: ", output_size)

# Build hidden layer
hidden_layer=[]
for i in range(hidden_size):
    hidden_neuron=[]
    for j in range(len(dictionary)):
        rand_float=random.uniform(-0.5, 0.5)
        hidden_neuron.append(rand_float)

    hidden_layer.append(hidden_neuron)

hidden_bias=[]
for i in range(hidden_size):
    rand_float=random.uniform(-0.5, 0.5)
    hidden_bias.append(rand_float)


# Build output later
output_layer=[]
for i in range (output_size):
    output_neuron=[]
    for j in range(hidden_size):
        rand_float=random.uniform(-0.5, 0.5)
        output_neuron.append(rand_float)
    output_layer.append(output_neuron)

output_bias=[]
for i in range(output_size):
    rand_float=random.uniform(-0.5, 0.5)
    output_bias.append(rand_float)



def build_bow(phrase,dictionary):
    bag=[]
    tokenized=word_tokenize(phrase.lower().translate(remove_punctuation))
    for token in dictionary:
        if token in tokenized:
            bag.append(1)
        else:
            bag.append(0)
    return bag
def optimize_bow(bag):
    optimized_bag=[]
    for item_index, item in enumerate(bag):
        if item == 1:
            optimized_bag.append(item_index)
    return optimized_bag
# Forward Pass
def forward_pass(optimized_bag, bag):
    # Hidden Layer
    hidden_output=[]
    for neuron_index, neuron in enumerate(hidden_layer):
        #neuron=[0.123, 0.456, ...]
        output=0

        scale = 1 / math.sqrt(len(optimized_bag)) if optimized_bag else 1.0
        for item_index in optimized_bag:
            # for float in previous layer
            output+=scale*neuron[item_index]


        output+=hidden_bias[neuron_index]
        output=activation(output)

        hidden_output.append(output)

    # Output Layer
    output_output=[]
    logits=[]
    for neuron_index, neuron in enumerate(output_layer):
        #neuron=[0.123, 0.456, ...]
        output=0

        
        for item_index, item in enumerate(hidden_output):
            # for float in previous layer
            output+=item*neuron[item_index]


        output+=output_bias[neuron_index]
        #output=activation(output)

        logits.append(output)
    output_output=softmax(logits)
    return output_output, hidden_output

def backprop(phrase, actual, hidden_output, bag, optimized_bag):
    # Find expected output
    expected=[]
    for tag in patterns:
        if phrase in tag:
            expected.append(0.9)
        else:
            expected.append(0.1/(len(patterns)-1))
    # Expected out, [0,0,0,1,0,0,0,0, ...]

    # Find output deltas
    output_deltas=[]
    for output_index, output in enumerate(actual):
            # For each number 
            expected_output=expected[output_index]
    
            error=expected_output-output
            output_delta=error
            output_deltas.append(output_delta)
    
    # Find hidden deltas
    hidden_deltas=[]
    for hidden_index, hidden in enumerate(hidden_output):
            # For each number in hidden_output
    
            hidden_error=0
            for delta_index, delta in enumerate(output_deltas):
                hidden_error+=delta*output_layer[delta_index][hidden_index]

            hidden_delta=hidden_error*hidden*(1-hidden)
            hidden_deltas.append(hidden_delta)

    # Adjust output layer

    for output_index, output in enumerate(actual):
        # For each number 
        output_delta=output_deltas[output_index]

        for value_index, hidden_value in enumerate(hidden_output):
            # For each value from the hidden layer/number of connects in each neuron
            output_layer[output_index][value_index]+=learning_rate*output_delta*hidden_value

        output_bias[output_index]+=learning_rate*output_delta

    # Adjust hidden layer

    for hidden_index, hidden in enumerate(hidden_output):
        # For each number 
        hidden_delta=hidden_deltas[hidden_index
                                   ]
        # The input to each of these weights is `scale`, not 1 (see forward_pass),
        # so the gradient carries the same factor. Leaving this at *1 trains the
        # hidden layer with an effective learning rate that varies with message
        # length -- longest messages get the biggest steps, which is backwards.
        scale = 1 / math.sqrt(len(optimized_bag)) if optimized_bag else 1.0
        for value_index in optimized_bag:
            # For each value from the hidden layer/number of connects in each neuron
            hidden_layer[hidden_index][value_index]+=learning_rate*hidden_delta*scale
        
        hidden_bias[hidden_index]+=learning_rate*hidden_delta

# Prepare data
training_data=[]
for tag_index, tag in enumerate(patterns):
    for phrase_index, phrase in enumerate(tag):
        bag=build_bow(phrase, dictionary)
        optimized_bag=optimize_bow(bag)
        training_data.append((bag, optimized_bag, phrase, tag_index))

# Build Eval

eval_intents=json.loads(open('intents.json').read())["eval"]
eval_tags=[]
for tag in eval_intents:
    eval_tags.append(tag["tag"])
eval_patterns=[]
for tag in eval_intents:
    tag_patterns=[]
    for pattern in tag["patterns"]:
        tag_patterns.append(pattern)
    eval_patterns.append(tag_patterns) 

eval_data=[]
for tag_index, tag in enumerate(eval_patterns):
    for phrase_index, phrase in enumerate(tag):
        bag=build_bow(phrase, dictionary)
        optimized_bag=optimize_bow(bag)
        eval_data.append((bag, optimized_bag, phrase, tag_index))




epoch=0
for i in range(epochs):
    random.shuffle(training_data)
    epoch_loss=0.0
    correct=0
    eval_loss=0.0
    eval_correct=0
    for item in training_data:
        bag, optimized_bag, phrase, tag_index=item
        actual, hidden=forward_pass(optimized_bag, bag)
        epoch_loss += -math.log(max(actual[tag_index], 1e-12))
        if actual.index(max(actual))==tag_index:
            correct+=1
        backprop(phrase,actual,hidden,bag,optimized_bag)


    for item in eval_data:
        bag, optimized_bag, phrase, tag_index=item
        actual, hidden=forward_pass(optimized_bag, bag)
        eval_loss += -math.log(max(actual[tag_index], 1e-12))
        if actual.index(max(actual)) == tag_index:
            eval_correct+=1

    
    epoch+=1
    print("Epoch: "+str(epoch))
    print("Loss: "+str(epoch_loss/len(training_data)))
    print("Correct: "+str(correct))
    print("Eval Loss: "+str(eval_loss/len(eval_data)))
    print("Eval Correct: "+str(eval_correct))



format = {
    "dictionary": dictionary,
    "tags": tags,
    "hidden_layer": hidden_layer,
    "hidden_bias": hidden_bias,
    "output_layer": output_layer,
    "output_bias": output_bias
}

with open("model.json", "w") as f:
    json.dump(format, f, indent=4)
    
