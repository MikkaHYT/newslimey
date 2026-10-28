import re

parameter_parser = lambda params: re.compile(
    r"--({})(?:\s+([\S\s]*?)(?=\s--|$))?".format("|".join(map(re.escape, params)))
)

link = re.compile(
    r"https?:\/\/(?:www\.)?[-a-zA-Z0-9@:%._\+~#=]{1,256}\.[a-zA-Z0-9()]{1,6}\b"
    r"(?:[-a-zA-Z0-9()@:%_\+.~#?&\/\/=]*(?:\.png|\.jpe?g|\.gif|\.jpg|))?"
)
