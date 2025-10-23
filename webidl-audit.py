#!/usr/bin/python3

# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at http://mozilla.org/MPL/2.0/.

import argparse
import re
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

# TODO Include test_interfaces.js in the example git command line.
#      dom/tests/mochitest/general/test_interfaces.js

# TODO By default, maybe run the git log command and process that, and then
#      allow optionally specifying the log file to skip that.

# TODO Making the git operations do a bunch of revisions at once surely will
#      be faster.

# Argument parsing.
def getArgs():
    argParser = argparse.ArgumentParser(description='''
    WebIDL review audit helper.

    Generate the log file with this command:
    git log --oneline --invert-grep --grep='webidl' -- dom/webidl/
    ''',
                                        formatter_class=argparse.RawTextHelpFormatter)
    argParser.add_argument("gitRepo", help="directory of the Firefox git repository")
    argParser.add_argument("filename", help="git log file name")
    return argParser.parse_args()


# Look up the given revision in the given git repository directory and return the date.
def revisionDate(repoDirName, revision):
    repo = Path(repoDirName).resolve()
    dateString = subprocess.check_output(
        ["git", "-C", str(repo), "show", "--no-patch", "--format=%cI", revision],
        text=True
    ).strip()

    # datetime doesn't handle Z as a shorthand for the time zone, so fix it.
    dateString = dateString.replace("Z", "+00:00")

    return datetime.fromisoformat(dateString)


# There are some commits from more than a few years ago that have strange
# formats. Rather than adding more strange cases to deal with them, just ignore
# them, as the main goal of this audit is to find recent problems. This doesn't
# need to be a precise calculation.
currentDate = datetime.now(timezone.utc)
# I picked 3 to dodge a specific commit from December 2021.
numOldYears = 3
def dateIsOld(previousDate):
    return currentDate - previousDate >= timedelta(days=numOldYears * 365)


# Do the initial classification of the summary lines.
backoutRE = "|".join(["Revert", "Backed out", "Back out", "back out",
                      "Backout", "backout", "BACKOUT", "Backing out"])
mergeRE = "|".join(["Merge", "merge"])

backoutMergeRegexp = f"^(?P<revision>[a-z0-9]+) ({backoutRE}|{mergeRE}) "
backoutMergePattern = re.compile(backoutMergeRegexp)
bugPattern = re.compile(r"^(?P<revision>[a-z0-9]+)\s+(Fix for )?(Bug|bug) (?P<bugno>\d+)")
unrecognizedPattern = re.compile("^(?P<revision>[a-z0-9]+) (?P<summary>.*)$")

bugs = []
numReverts = 0
numOldUnrecognized = 0

args = getArgs()
file_path = Path(args.filename)
with file_path.open(encoding="utf-8") as f:
    for line in f:
        line = line.strip()

        match = bugPattern.match(line)
        if match:
            bugs.append((match.group("revision"), match.group("bugno"), line))
            continue

        match = backoutMergePattern.match(line)
        if match:
            numReverts += 1
            continue

        match = unrecognizedPattern.match(line)
        if match:
            if dateIsOld(revisionDate(args.gitRepo, match.group("revision"))):
                numOldUnrecognized += 1
                continue
            else:
                sys.stderr.write(f'Error: non-old unrecognized line: {match.group("summary")}\n')
                exit(-1)

        sys.stderr.write(f'Error: hashless line: {match.group("summary")}\n')
        exit(-1)


# In this section, we parse the list of reviewers, and check if any are DOM peers.
reviewerPattern = re.compile(r"^(r=|sr=)?(?P<reviewer>[a-zA-Z0-9\-\.\_]+)$")

# This includes a lot of former peers, to try to prune out as many commits
# as possible before we have to check the date in git. There's probably not
# much risk of one of them suddenly r+ing things they shouldn't.
webIDLPeersRE = "|".join([
    "asuth", "baku", "bent", "bholley", "billm", "bkelly", "bz", "bzbarsky",
    "echen", "edgar", "ehsan", "emilio", "farre", "hsivonen", "jst", "khuey",
    "mccr8", "mounir", "mrbkap", "nika", "Nika", "peterv", "qdot", "saschanaz",
    "sefeng", "sicking", "smaug", "tschuster"])
webIDLPeersPattern = re.compile(f"^({webIDLPeersRE})$")

def parseReviewers(rString):
    for r in rString.split(","):
        r = r.strip(" ")
        r = r.rstrip(" .])")
        match = reviewerPattern.match(r)
        if match:
            r = match.group("reviewer")
        else:
            if r == "emilio DONTBUILD":
                # Bug 1940098 has a weird DONTBUILD at the end.
                r = "emilio"
            else:
                return "BAD_PARSE"
        match = webIDLPeersPattern.match(r)
        if match:
            return "OK"
    return "MISSING"


rEqualPattern = re.compile(r"r\=(?P<reviewer>.*)$")

numHasPeer = 0
numOldMissing = 0
numOldReviewerless = 0
numOldUnparsableReviewers = 0
numPeerAuthored = 0

bugsMissingReview = []

for (revision, bugnumber, line) in bugs:
    match = rEqualPattern.search(line)
    if match:
        reviewers = parseReviewers(match.group("reviewer"))
        if reviewers == "OK":
            numHasPeer += 1
            continue
        if reviewers == "BAD_PARSE":
            if dateIsOld(revisionDate(args.gitRepo, revision)):
                numOldUnparsableReviewers += 1
                continue
            else:
                sys.stderr.write(f"Error: couldn't parse reviewers for non-old bug: {line}\n")
                exit(-1)
        if reviewers == "MISSING":
            if dateIsOld(revisionDate(args.gitRepo, revision)):
                numOldMissing += 1
                continue
            bugsMissingReview.append((revision, bugnumber, line))
            continue
        sys.stderr.write(f"Error: unexpected parseReviewers return value {reviewers}: {line}\n")
        exit(-1)
    elif dateIsOld(revisionDate(args.gitRepo, revision)):
        numOldReviewerless += 1
        continue
    elif bugnumber == "1968400":
        # Bug 1968400 landed 2025-05-27, without a reviewer string.
        # smaug wrote it, so it is okay.
        numPeerAuthored += 1
        continue
    else:
        sys.stderr.write(f"Error: non-old bug without reviewer string: {line}\n")
        exit(-1)

bugs = []

# Next, we check the authors of patches that aren't reviewed by WebIDL peers.
# If the author is a WebIDL peer, that's probably fine.
# Also ignore known issues.

# Return the email of the revision author, in the given repository.
def revisionAuthor(repoDirName, revision):
    repo = Path(repoDirName).resolve()
    author = subprocess.check_output(
        ["git", "-C", str(repo), "show", "--no-patch", '--format=%ce', revision],
        text=True
    ).strip()
    return author

# Only cover the few cases we actually need.
webIDLPeersEmailRE = "|".join(["emilio@crisal.io", "sefeng@mozilla.com"])
webIDLPeersEmailPattern = re.compile(f"^({webIDLPeersEmailRE})$")

numKnownMissing = 0
numUnknownMissing = 0

for (revision, bugnumber, line) in bugsMissingReview:
    author = revisionAuthor(args.gitRepo, revision)
    matches = webIDLPeersEmailPattern.match(author)
    if matches:
        numPeerAuthored += 1
        continue
    if bugnumber == "1966190":
        # Bug 1966190: WebIDL reviewer was removed by author because it was
        # comment-only. It was also for Glean, so not actually a web thing,
        # so not really a problem.
        numKnownMissing += 1
        continue
    if bugnumber == "1979610":
        # Bug 1979610: This got overlooked at the time of landing due to some
        # weirdness with the Herald rule. (See bug 1986775.) It was reviewed
        # post-landing by Emilio.
        numKnownMissing += 1
        continue

    numUnknownMissing += 1
    sys.stderr.write(f'MISSING WebIDL review for bug {bugnumber}: {line}')

if numUnknownMissing > 0:
    sys.stderr.write(f'!!! Unknown non-old patches missing WebIDL review !!!')
    exit(-1)

print(f"Patches with WebIDL peer review: {numHasPeer}")
print(f"Patches missing WebIDL peer review, known issue: {numKnownMissing}")
print(f"Patches missing WebIDL peer review, but authored by peer: {numPeerAuthored}")
print(f"Old patches missing WebIDL peer review: {numOldMissing}")
print(f"Old patches without parsable reviewer strings: {numOldUnparsableReviewers}")
print(f"Old patches without reviewer strings: {numOldReviewerless}")
print()
print(f"Patches that are backouts: {numReverts}")
print(f"Old unrecognized summaries: {numOldUnrecognized}")
print()
print(f"(Old means more than {numOldYears} years old.)")
