## What stages have cost the executor

Measured, across every run of this project. The figure is how much context the executor carried on that stage: each attempt's high-water mark, added across the attempts it took. A stage that needed three passes really did load context three times.

**Read them against each other, not against a limit.** The useful fact is that one stage cost three times another, not what fraction of a window it used — the window is not what bounds a stage. What bounds it is what a failure costs to redraw, since a stage lands completely or not at all, and what can be judged as one diff. Size against the entries nearest the work you are drawing: the figure is dominated by fixed overhead, so cost tracks the size of the files far more than their number.

A number on its own is not a comparison. Each line is named by the commit that landed it, and `git_show` on that sha **with no path** answers with the instruction that stage was given and how many lines it changed in each file. Use it on the closest one or two before sizing something unfamiliar — a figure you cannot picture the work behind is not calibration. The commit is where that account lives.

$lines
