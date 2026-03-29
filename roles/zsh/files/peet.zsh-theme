function git_prompt_info() {
	ZSH_THEME_GIT_PROMPT_CLEAN="%{$fg[green]%}"
	local ref
	ref=$(git symbolic-ref --short -q HEAD 2> /dev/null)
	if [ -z "$ref" ]; then
		# We are in detached head, or not in a git repository
		ref=$(git rev-parse --short HEAD 2> /dev/null) || return
		ZSH_THEME_GIT_PROMPT_CLEAN="%{$fg[yellow]%}"
	fi
	echo " $(parse_git_dirty)$ZSH_THEME_GIT_PROMPT_PREFIX$(git_current_branch)$ZSH_THEME_GIT_PROMPT_SUFFIX"
}

PROMPT=$'%{$bold_color%}%(!.%{$FG[001]%}.%{$FG[002]%})┌──[%{$reset_color%}%{$FG[003]%}%n%{$reset_color%}%{$FG[246]%}@%{$reset_color%}%{$FG[208]%}%m%{$reset_color%}%{$FG[246]%}, %T%{$reset_color%}%{$bold_color%}%(!.%{$FG[001]%}.%{$FG[002]%})]%{$reset_color%} %{$FG[012]%}%/%{$reset_color%}$(git_prompt_info)%{$reset_color%}%(?.%{$fg[green]%} \u2713%{$reset_color%}.%{$fg[red]%} \u2717%{$reset_color%} %{$FG[246]%}(%?%)%{$reset_color%})
%{$bold_color%}%(!.%{$FG[001]%}.%{$FG[002]%})└─%(!.#.$)%{$reset_color%} '

PROMPT2="%{$fg_bold[black]%}%_> %{$reset_color%}"

ZSH_THEME_GIT_PROMPT_PREFIX="["
ZSH_THEME_GIT_PROMPT_SUFFIX="]"
ZSH_THEME_GIT_PROMPT_DIRTY="%{$fg[red]%}"
